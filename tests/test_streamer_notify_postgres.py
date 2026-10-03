"""
Real PostgreSQL integration tests for @sonya_group_bot streamer completion
notifications (migration 015 + scripts/streamer_notify.py +
scripts/prod_job_store.py's claim_streamer_notification/mark_streamer_
notification_sent/mark_streamer_notification_failed).

telegram_bot.send_message is monkeypatched at every call site in this
file -- per scripts/streamer_notify.py's own module docstring, this pass
never sends a real Telegram message. Real Postgres is used for everything
else (batch/user rows, the atomic claim, concurrency) -- same approach as
tests/test_streamer_batches_postgres.py.

Skipped automatically unless DATABASE_URL is set.
Local run:
    export DATABASE_URL="postgresql://localhost/sonya_test"
    python scripts/run_migrations.py
    pytest tests/test_streamer_notify_postgres.py -v
"""
from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres streamer notify test skipped locally",
)

from scripts import auth_store, streamer_notify
from scripts.prod_job_store import _NOTIFY_CLAIM_LEASE_SECONDS, _now


@pytest.fixture()
def store():
    from scripts import prod_job_store
    return prod_job_store


def _create_user():
    return auth_store.create_user(f"streamer-notify-{uuid.uuid4()}@example.com")


def _link_telegram(user_id: str, chat_id: int | None = None) -> int:
    chat_id = chat_id if chat_id is not None else int(uuid.uuid4().int % 1_000_000_000)
    tg_user_id = int(uuid.uuid4().int % 1_000_000_000)
    conn = auth_store._get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET telegram_linked=TRUE, telegram_chat_id=%s, "
                    "telegram_user_id=%s, telegram_linked_at=NOW() WHERE id=%s",
                    (chat_id, tg_user_id, user_id),
                )
    finally:
        conn.close()
    return chat_id


def _unlink_telegram(user_id: str) -> None:
    conn = auth_store._get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET telegram_linked=FALSE, telegram_chat_id=NULL, "
                    "telegram_user_id=NULL, telegram_linked_at=NULL WHERE id=%s",
                    (user_id,),
                )
    finally:
        conn.close()


def _make_batch(store, user_id: str, status: str, notify_enabled: bool = True) -> str:
    batch = store.create_streamer_batch(user_id, preset_snapshot={"telegram_notify_enabled": notify_enabled})
    if status != "queued":
        store.set_streamer_batch_status(batch["id"], status)
    return str(batch["id"])


def _push_claim_into_the_past(batch_id: str, seconds_ago: int) -> None:
    """Test-only time travel for the claim lease -- avoids an actual sleep()."""
    conn = auth_store._get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE streamer_batches SET telegram_notify_claimed_at = %s WHERE id = %s",
                    (_now() - timedelta(seconds=seconds_ago), batch_id),
                )
    finally:
        conn.close()


@pytest.fixture()
def fake_sender(monkeypatch):
    """Records every send_message call; accepts (returns True) by default."""
    calls = []

    def _fake(chat_id, text, button_text=None, button_url=None):
        calls.append({"chat_id": chat_id, "text": text, "button_text": button_text, "button_url": button_url})
        return _fake.result

    _fake.result = True
    _fake.calls = calls
    monkeypatch.setattr(streamer_notify.telegram_bot, "send_message", _fake)
    return _fake


# ── A/D/status gating ────────────────────────────────────────────────────

def test_ready_enabled_linked_sends_once(store, fake_sender):
    user = _create_user()
    chat_id = _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert len(fake_sender.calls) == 1
    assert fake_sender.calls[0]["chat_id"] == chat_id
    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notified_at"] is not None


def test_partially_failed_enabled_linked_sends_once(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "partially_failed", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert len(fake_sender.calls) == 1
    assert "Большинство клипов готово" in fake_sender.calls[0]["text"]


def test_failed_status_never_sends(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "failed", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert fake_sender.calls == []
    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notified_at"] is None


def test_cancelled_status_never_sends(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "cancelled", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert fake_sender.calls == []


def test_notify_disabled_never_sends(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=False)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert fake_sender.calls == []
    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notified_at"] is None


# ── B/C: linked-state timing policy ─────────────────────────────────────

def test_unlinked_before_completion_no_send_batch_unaffected(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "queued", notify_enabled=True)
    _unlink_telegram(user["id"])  # unlinked before the batch ever completes
    store.set_streamer_batch_status(batch_id, "ready")

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert fake_sender.calls == []
    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["status"] == "ready"  # batch itself completely unaffected
    assert batch["telegram_notified_at"] is None


def test_never_linked_at_all_no_send(store, fake_sender):
    user = _create_user()
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert fake_sender.calls == []


def test_linked_after_creation_before_completion_sends(store, fake_sender):
    user = _create_user()
    batch_id = _make_batch(store, user["id"], "queued", notify_enabled=True)  # not linked yet at creation
    chat_id = _link_telegram(user["id"])  # links before completion
    store.set_streamer_batch_status(batch_id, "ready")

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert len(fake_sender.calls) == 1
    assert fake_sender.calls[0]["chat_id"] == chat_id


# ── E: temporary failure / retry ────────────────────────────────────────

def test_temporary_failure_leaves_notified_at_null_and_batch_ready(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)
    fake_sender.result = False  # simulated Telegram API failure

    streamer_notify.notify_streamer_batch_completion(batch_id)

    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notified_at"] is None
    assert batch["status"] == "ready"  # Telegram failure never touches batch status
    assert batch["telegram_notify_last_error"] == "telegram_send_failed"
    assert batch["telegram_notify_attempts"] == 1


def test_retry_after_failure_succeeds_once_lease_expires(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)
    fake_sender.result = False
    streamer_notify.notify_streamer_batch_completion(batch_id)
    assert len(fake_sender.calls) == 1  # attempted once, rejected

    # Immediate retry: still within the lease window -- must NOT re-attempt.
    streamer_notify.notify_streamer_batch_completion(batch_id)
    assert len(fake_sender.calls) == 1  # still just the one attempt

    # Lease has "expired" -- next call must retry and this time succeed.
    _push_claim_into_the_past(batch_id, _NOTIFY_CLAIM_LEASE_SECONDS + 5)
    fake_sender.result = True
    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert len(fake_sender.calls) == 2
    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notified_at"] is not None
    assert batch["telegram_notify_attempts"] == 2


# ── F/G: concurrency, no-duplicate-on-replay ────────────────────────────

def test_concurrent_notify_calls_only_one_sender(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _i: streamer_notify.notify_streamer_batch_completion(batch_id), range(8)))

    assert len(fake_sender.calls) == 1


def test_success_then_repeated_calls_no_duplicate_send(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)
    streamer_notify.notify_streamer_batch_completion(batch_id)
    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert len(fake_sender.calls) == 1


# ── Claim primitive: stale reclaim ──────────────────────────────────────

def test_stale_claim_becomes_reclaimable(store):
    user = _create_user()
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    assert store.claim_streamer_notification(batch_id) is True
    assert store.claim_streamer_notification(batch_id) is False  # active lease -- can't claim again

    _push_claim_into_the_past(batch_id, _NOTIFY_CLAIM_LEASE_SECONDS + 5)
    assert store.claim_streamer_notification(batch_id) is True  # lease expired -- reclaimable

    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notify_attempts"] == 2  # both successful claims counted


def test_claim_fails_once_already_notified(store):
    user = _create_user()
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)
    assert store.claim_streamer_notification(batch_id) is True
    store.mark_streamer_notification_sent(batch_id)

    assert store.claim_streamer_notification(batch_id) is False  # permanent, not just a lease


def test_claim_fails_for_ineligible_status(store):
    user = _create_user()
    batch_id = _make_batch(store, user["id"], "generating", notify_enabled=True)
    assert store.claim_streamer_notification(batch_id) is False


# ── Deep link / message content ─────────────────────────────────────────

def test_deep_link_includes_batch_id(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    call = fake_sender.calls[0]
    assert call["button_url"] == f"https://sonya.group/streamer-mode.html?batch={batch_id}"
    assert call["button_text"] == "Открыть клипы"
    # No technical details ever included.
    for forbidden in ("job_id", "s3://", "S3", "GPU", "traceback"):
        assert forbidden not in call["text"]


# ── Wrong-user chat_id isolation ─────────────────────────────────────────

def test_wrong_users_chat_id_never_used(store, fake_sender):
    owner = _create_user()
    other = _create_user()
    owner_chat_id = _link_telegram(owner["id"], chat_id=111111)
    _link_telegram(other["id"], chat_id=222222)
    batch_id = _make_batch(store, owner["id"], "ready", notify_enabled=True)

    streamer_notify.notify_streamer_batch_completion(batch_id)

    assert len(fake_sender.calls) == 1
    assert fake_sender.calls[0]["chat_id"] == owner_chat_id
    assert fake_sender.calls[0]["chat_id"] != 222222


# ── Secret hygiene ───────────────────────────────────────────────────────

def test_failure_error_string_never_leaks_exception_detail(store, monkeypatch):
    """
    send_message() itself only ever returns True/False (see telegram_bot.
    py) -- it never surfaces an exception message to its caller. This
    confirms mark_streamer_notification_failed only ever receives the one
    fixed, safe, code-controlled string this module uses -- never
    anything derived from a real exception (which could embed the bot
    token in a request URL, per telegram_bot.py's own docstring).
    """
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    def _raises(*a, **kw):
        raise RuntimeError("http://api.telegram.org/botSECRET-TOKEN-VALUE/sendMessage boom")

    monkeypatch.setattr(streamer_notify.telegram_bot, "send_message", _raises)

    streamer_notify.notify_streamer_batch_completion(batch_id)  # must not raise

    batch = store.get_streamer_batch_by_id(batch_id)
    # notify_streamer_batch_completion() caught the exception at the OUTER
    # try/except (send_message raised instead of returning False) -- so no
    # mark_streamer_notification_failed call happened at all here, and
    # critically, nothing about the exception text ever reached the DB.
    assert batch["telegram_notify_last_error"] is None or "SECRET-TOKEN-VALUE" not in batch["telegram_notify_last_error"]


def test_failure_error_string_never_contains_token_via_normal_failure_path(store, fake_sender):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)
    fake_sender.result = False

    streamer_notify.notify_streamer_batch_completion(batch_id)

    batch = store.get_streamer_batch_by_id(batch_id)
    assert batch["telegram_notify_last_error"] == "telegram_send_failed"


def test_unexpected_exception_never_propagates_and_logs_only_type_name(store, monkeypatch, caplog):
    user = _create_user()
    _link_telegram(user["id"])
    batch_id = _make_batch(store, user["id"], "ready", notify_enabled=True)

    def _raises(*a, **kw):
        raise RuntimeError("http://api.telegram.org/botSECRET-TOKEN-VALUE/sendMessage boom")

    monkeypatch.setattr(streamer_notify.telegram_bot, "send_message", _raises)

    with caplog.at_level(logging.WARNING, logger="scripts.streamer_notify"):
        streamer_notify.notify_streamer_batch_completion(batch_id)  # must not raise

    for record in caplog.records:
        assert "SECRET-TOKEN-VALUE" not in record.getMessage()
        assert "api.telegram.org" not in record.getMessage()
