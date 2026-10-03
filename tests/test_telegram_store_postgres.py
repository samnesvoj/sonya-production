"""
Real PostgreSQL integration tests for Telegram account linking
(migration 013): token issuance/consumption, account linking/unlinking,
and webhook update-id idempotency.

Skipped automatically unless DATABASE_URL is set.
Local run (same DB the rest of this project's _postgres tests already use):
    export DATABASE_URL="postgresql://localhost/sonya_test"
    python scripts/run_migrations.py
    pytest tests/test_telegram_store_postgres.py -v

Real foreign keys are involved (telegram_link_tokens.user_id ->
users(id)), so every test creates a real user row via auth_store.create_user()
first, not a bare random uuid.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres telegram linking test skipped locally",
)


@pytest.fixture()
def store():
    from scripts import telegram_store
    return telegram_store


@pytest.fixture()
def user_id():
    from scripts import auth_store
    user = auth_store.create_user(f"telegram-test-{uuid.uuid4()}@example.com")
    return user["id"]


def _get_conn():
    from scripts.telegram_store import _get_conn as gc
    return gc()


def _rand_tg_id() -> int:
    """
    A fresh, effectively-unique fake Telegram user/chat id per call — NOT
    a small fixed literal. ux_users_telegram_user_id (migration 013) is a
    real UNIQUE index and these tests don't clean up after themselves
    (matching every other _postgres test file's own convention), so a
    fixed value like `1` would collide with whatever a PREVIOUS run of
    this same file already committed and fail with an unrelated-looking
    IntegrityError the second time the suite runs against the same DB.
    """
    return int(uuid.uuid4().int % (2**31))


# ── Token creation ───────────────────────────────────────────────────────

def test_token_created(store, user_id):
    result = store.create_link_token(user_id)
    assert result["token"]
    assert len(result["token"]) > 20  # secrets.token_urlsafe(32) is long
    assert result["expires_at"] > datetime.now(timezone.utc)


def test_db_stores_hash_not_plaintext(store, user_id):
    from scripts.telegram_security import hash_link_token

    result = store.create_link_token(user_id)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM telegram_link_tokens WHERE id=%s", (result["id"],))
            row = cur.fetchone()
    finally:
        conn.close()

    assert row["token_hash"] != result["token"]  # never the raw value
    assert row["token_hash"] == hash_link_token(result["token"])


# ── Token consumption ────────────────────────────────────────────────────

def test_successful_consume_returns_correct_user_id(store, user_id):
    result = store.create_link_token(user_id)
    consumed_user_id = store.consume_link_token(result["token"])
    assert consumed_user_id == user_id


def test_invalid_token_rejected(store):
    with pytest.raises(store.TelegramLinkTokenInvalidError):
        store.consume_link_token("this-token-was-never-issued")


def test_used_token_rejected(store, user_id):
    result = store.create_link_token(user_id)
    store.consume_link_token(result["token"])  # first use succeeds

    with pytest.raises(store.TelegramLinkTokenUsedError):
        store.consume_link_token(result["token"])  # replay rejected


def test_expired_token_rejected(store, user_id):
    conn = _get_conn()
    token_id = str(uuid.uuid4())
    from scripts.telegram_security import generate_link_token, hash_link_token
    raw_token = generate_link_token()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO telegram_link_tokens (id, user_id, token_hash, created_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (token_id, user_id, hash_link_token(raw_token),
                     datetime.now(timezone.utc) - timedelta(minutes=20),
                     datetime.now(timezone.utc) - timedelta(minutes=5)),
                )
    finally:
        conn.close()

    with pytest.raises(store.TelegramLinkTokenExpiredError):
        store.consume_link_token(raw_token)


# ── Account linking ──────────────────────────────────────────────────────

def test_successful_link_sets_flags_and_linked_at(store, user_id):
    from scripts import auth_store

    tg_id = _rand_tg_id()
    store.link_telegram_account(user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)

    user = auth_store.get_user_by_id(user_id)
    assert user["telegram_linked"] is True
    assert user["telegram_user_id"] == tg_id
    assert user["telegram_chat_id"] == tg_id
    assert user["telegram_linked_at"] is not None


def test_sonya_user_cannot_silently_attach_another_telegram_user(store, user_id):
    first_tg_id, second_tg_id = _rand_tg_id(), _rand_tg_id()
    store.link_telegram_account(user_id, telegram_user_id=first_tg_id, telegram_chat_id=first_tg_id)

    with pytest.raises(store.TelegramAccountAlreadyLinkedError):
        store.link_telegram_account(user_id, telegram_user_id=second_tg_id, telegram_chat_id=second_tg_id)


def test_same_telegram_cannot_silently_attach_to_another_sonya_user(store, user_id):
    from scripts import auth_store
    other_user_id = auth_store.create_user(f"other-{uuid.uuid4()}@example.com")["id"]
    tg_id = _rand_tg_id()

    store.link_telegram_account(user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)

    with pytest.raises(store.TelegramAlreadyLinkedToAnotherUserError):
        store.link_telegram_account(other_user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)

    # First user's link must survive the rejected second attempt.
    first = auth_store.get_user_by_id(user_id)
    assert first["telegram_user_id"] == tg_id


def test_relinking_same_pair_is_idempotent(store, user_id):
    tg_id = _rand_tg_id()
    store.link_telegram_account(user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)
    from scripts import auth_store
    first = auth_store.get_user_by_id(user_id)

    store.link_telegram_account(user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)
    second = auth_store.get_user_by_id(user_id)

    assert first["telegram_linked_at"] == second["telegram_linked_at"]  # not bumped


# ── Unlink ────────────────────────────────────────────────────────────────

def test_unlink_clears_all_telegram_fields(store, user_id):
    from scripts import auth_store
    tg_id = _rand_tg_id()
    store.link_telegram_account(user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)

    store.unlink_telegram_account(user_id)

    user = auth_store.get_user_by_id(user_id)
    assert user["telegram_linked"] is False
    assert user["telegram_user_id"] is None
    assert user["telegram_chat_id"] is None
    assert user["telegram_linked_at"] is None


def test_unlink_then_relink_to_a_different_telegram_account_works(store, user_id):
    from scripts import auth_store
    first_tg_id, second_tg_id = _rand_tg_id(), _rand_tg_id()
    store.link_telegram_account(user_id, telegram_user_id=first_tg_id, telegram_chat_id=first_tg_id)
    store.unlink_telegram_account(user_id)

    store.link_telegram_account(user_id, telegram_user_id=second_tg_id, telegram_chat_id=second_tg_id)

    user = auth_store.get_user_by_id(user_id)
    assert user["telegram_user_id"] == second_tg_id


# ── Webhook update idempotency (claim / process / retry) ────────────────

def _fresh_update_id() -> int:
    return int(uuid.uuid4().int % (2**31))


def test_claim_update_new_update_returns_process(store):
    assert store.claim_update(_fresh_update_id()) == "process"


def test_successfully_processed_update_ignored_on_replay(store):
    update_id = _fresh_update_id()
    assert store.claim_update(update_id) == "process"
    store.mark_update_processed(update_id)

    # Telegram redelivers the same update_id after a successful 200.
    assert store.claim_update(update_id) == "skip"


def test_failed_handler_does_not_permanently_consume_update(store):
    """A claimed-but-never-marked-processed update (handler raised, or
    the process died) must not be treated as done -- it stays
    outstanding, just not immediately re-claimable within the lease
    window (see the next test for the retry-after-lease-expiry case)."""
    update_id = _fresh_update_id()
    assert store.claim_update(update_id) == "process"
    # Handler "fails" here -- mark_update_processed() is deliberately
    # never called, mirroring an exception escaping _process_start_command.

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT processed_at FROM telegram_updates WHERE update_id=%s", (update_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["processed_at"] is None  # never silently marked done


def test_retry_after_failure_processes_it_again(store):
    """Once the claim lease has expired (simulating enough time passing
    for Telegram's own redelivery to arrive), claim_update() must return
    "process" again -- a failed attempt is genuinely retryable, not a
    dead end."""
    update_id = _fresh_update_id()
    assert store.claim_update(update_id) == "process"  # first attempt, handler "fails"

    # Simulate lease expiry directly (same technique as
    # test_expired_token_rejected above) rather than sleeping in a test.
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE telegram_updates SET claimed_at=%s WHERE update_id=%s",
                    (datetime.now(timezone.utc) - timedelta(seconds=store._UPDATE_CLAIM_LEASE_SECONDS + 5), update_id),
                )
    finally:
        conn.close()

    assert store.claim_update(update_id) == "process"  # retry succeeds


def test_concurrent_duplicate_claim_only_one_gets_process(store):
    """Many genuinely concurrent claim_update() calls for the SAME new
    update_id -- exactly one must get "process", every other must get
    "skip". Real threads against the real DB, not a mocked race."""
    from concurrent.futures import ThreadPoolExecutor

    update_id = _fresh_update_id()
    n = 8

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: store.claim_update(update_id), range(n)))

    assert results.count("process") == 1
    assert results.count("skip") == n - 1


# ── Token + account-linking atomicity (link_via_token) ───────────────────

def test_link_via_token_links_and_consumes_together(store, user_id):
    from scripts import auth_store

    token = store.create_link_token(user_id)
    tg_id = _rand_tg_id()

    result = store.link_via_token(token["token"], tg_id, tg_id)

    assert result["telegram_user_id"] == tg_id
    user = auth_store.get_user_by_id(user_id)
    assert user["telegram_linked"] is True

    # Repeated successful use is still impossible.
    with pytest.raises(store.TelegramLinkTokenUsedError):
        store.link_via_token(token["token"], tg_id, tg_id)


def test_link_via_token_conflict_does_not_consume_the_token(store, user_id):
    """RELIABILITY requirement: token becomes used ONLY together with a
    successful link. A rejection (here: this SONYA user already linked to
    someone else) must leave the token itself still usable -- e.g. for a
    later /start after the user resolves the conflict."""
    from scripts import auth_store

    existing_tg_id, new_tg_id = _rand_tg_id(), _rand_tg_id()
    store.link_telegram_account(user_id, telegram_user_id=existing_tg_id, telegram_chat_id=existing_tg_id)

    token = store.create_link_token(user_id)
    with pytest.raises(store.TelegramAccountAlreadyLinkedError):
        store.link_via_token(token["token"], new_tg_id, new_tg_id)

    # Token was NOT consumed by the rejected attempt.
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT used_at FROM telegram_link_tokens WHERE id=%s", (token["id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["used_at"] is None

    # The original link is untouched by the rejected attempt.
    user = auth_store.get_user_by_id(user_id)
    assert user["telegram_user_id"] == existing_tg_id


def test_link_via_token_telegram_conflict_does_not_consume_the_token(store, user_id):
    """Same guarantee, other direction: the telegram_user_id already
    belongs to a different SONYA account."""
    from scripts import auth_store
    other_user_id = auth_store.create_user(f"other-{uuid.uuid4()}@example.com")["id"]
    tg_id = _rand_tg_id()
    store.link_telegram_account(other_user_id, telegram_user_id=tg_id, telegram_chat_id=tg_id)

    token = store.create_link_token(user_id)
    with pytest.raises(store.TelegramAlreadyLinkedToAnotherUserError):
        store.link_via_token(token["token"], tg_id, tg_id)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT used_at FROM telegram_link_tokens WHERE id=%s", (token["id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row["used_at"] is None


def test_concurrent_duplicate_link_via_token_links_exactly_once(store, user_id):
    """Concurrent duplicate delivery of the same /start (same raw token)
    cannot execute the actual account linking twice -- link_via_token()'s
    own atomic token consumption is what guarantees this, independent of
    update_id claiming. Real threads against the real DB."""
    from concurrent.futures import ThreadPoolExecutor

    token = store.create_link_token(user_id)
    tg_id = _rand_tg_id()
    n = 8

    def attempt(_):
        try:
            store.link_via_token(token["token"], tg_id, tg_id)
            return "ok"
        except store.TelegramLinkTokenUsedError:
            return "used"

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    assert results.count("ok") == 1
    assert results.count("used") == n - 1


# ── Abuse guard: creating a new token invalidates old unused ones ───────

def test_new_token_invalidates_previous_unused_tokens(store, user_id):
    first = store.create_link_token(user_id)
    second = store.create_link_token(user_id)

    with pytest.raises(store.TelegramLinkTokenExpiredError):
        store.consume_link_token(first["token"])

    # The newest token is still fully valid.
    consumed_user_id = store.consume_link_token(second["token"])
    assert consumed_user_id == user_id
