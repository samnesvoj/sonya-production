"""
POST /api/telegram/link-token, POST /api/telegram/webhook,
POST /api/telegram/unlink, GET /api/telegram/status.

No real Postgres, no real Telegram Bot API calls — scripts.telegram_store
and scripts.telegram_bot are monkeypatched at their call sites in
scripts.telegram_routes, same pattern as tests/test_generation_jobs_from_url.py
uses for url_ingest. See tests/test_telegram_store_postgres.py for the
real-DB linking/token tests this file doesn't duplicate.
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from scripts import auth_store, telegram_bot, telegram_routes, telegram_store
from scripts.auth_security import hash_session_token
from tests.conftest import make_session, make_user

WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


def _login(client, monkeypatch):
    user = make_user()
    token = "raw-session-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash",
                         lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id",
                         lambda uid: user if uid == user["id"] else None)
    client.cookies.set("sonya_session", token)
    return user


# ── POST /api/telegram/link-token ────────────────────────────────────────

def test_link_token_requires_authenticated_session(client):
    resp = client.post("/api/telegram/link-token")
    assert resp.status_code == 401


def test_link_token_response_shape(client, monkeypatch):
    user = _login(client, monkeypatch)
    monkeypatch.setattr(
        telegram_store, "create_link_token",
        lambda uid: {"token": "raw-token-abc123", "id": "tok-1", "expires_at": datetime(2030, 1, 1)},
    )

    resp = client.post("/api/telegram/link-token")

    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_username"] == "sonya_group_bot"
    assert body["deep_link"] == "https://t.me/sonya_group_bot?start=raw-token-abc123"
    assert "expires_at" in body


# ── POST /api/telegram/webhook ───────────────────────────────────────────

def test_webhook_rejects_missing_secret(client):
    resp = client.post("/api/telegram/webhook", json={"update_id": 1})
    assert resp.status_code == 403


def test_webhook_rejects_wrong_secret(client):
    resp = client.post(
        "/api/telegram/webhook", json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert resp.status_code == 403


def test_webhook_requires_update_id(client):
    resp = client.post("/api/telegram/webhook", json={}, headers=WEBHOOK_HEADERS)
    assert resp.status_code == 400


def test_webhook_duplicate_update_id_ignored(client, monkeypatch):
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "skip")
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 1, "message": {"text": "/start tok", "chat": {"id": 1}, "from": {"id": 1}}},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert sent == []  # never even looked at the message body


def test_webhook_ignores_non_start_messages(client, monkeypatch):
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: None)
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 2, "message": {"text": "hello", "chat": {"id": 1}, "from": {"id": 1}}},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert sent == []


def test_webhook_start_without_token_prompts_user(client, monkeypatch):
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    marked = []
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: marked.append(uid))
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 3, "message": {"text": "/start", "chat": {"id": 42}, "from": {"id": 42}}},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert len(sent) == 1 and sent[0][0] == 42
    assert "SONYA" in sent[0][1]
    assert marked == [3]  # a definitive (non-retryable) outcome still marks processed


def test_webhook_successful_start_links_account_and_replies(client, monkeypatch):
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    marked = []
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: marked.append(uid))
    linked = []

    def _fake_link_via_token(raw_token, telegram_user_id, telegram_chat_id):
        linked.append((raw_token, telegram_user_id, telegram_chat_id))
        return {"id": "user-abc"}
    monkeypatch.setattr(telegram_store, "link_via_token", _fake_link_via_token)
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 4, "message": {"text": "/start good-token", "chat": {"id": 99}, "from": {"id": 555}}},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert linked == [("good-token", 555, 99)]
    assert len(sent) == 1 and sent[0][0] == 99
    assert "подключён" in sent[0][1].lower()
    assert marked == [4]


@pytest.mark.parametrize("exc_name,expected_substring", [
    ("TelegramLinkTokenExpiredError", "устарела"),
    ("TelegramLinkTokenUsedError", "использована"),
    ("TelegramLinkTokenInvalidError", "распознать"),
])
def test_webhook_token_rejection_sends_clear_non_technical_message(client, monkeypatch, exc_name, expected_substring):
    exc_cls = getattr(telegram_store, exc_name)

    def _raise(*a, **kw):
        raise exc_cls("boom")
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    marked = []
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: marked.append(uid))
    monkeypatch.setattr(telegram_store, "link_via_token", _raise)
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 5, "message": {"text": "/start bad-token", "chat": {"id": 7}, "from": {"id": 7}}},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert len(sent) == 1
    assert expected_substring in sent[0][1].lower()
    # No technical error class names / stack details leaked to the user.
    assert exc_name not in sent[0][1]
    assert "traceback" not in sent[0][1].lower()
    # A definitive rejection still marks the update processed — no reason
    # for Telegram to keep redelivering a message that will always be
    # rejected the same way.
    assert marked == [5]


def test_webhook_already_linked_conflicts_do_not_crash(client, monkeypatch):
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: None)

    def _raise(*a, **kw):
        raise telegram_store.TelegramAlreadyLinkedToAnotherUserError("boom")
    monkeypatch.setattr(telegram_store, "link_via_token", _raise)
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 6, "message": {"text": "/start tok", "chat": {"id": 7}, "from": {"id": 7}}},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert len(sent) == 1
    assert "уже подключён" in sent[0][1].lower()


def test_webhook_transient_handler_failure_does_not_mark_processed(client, monkeypatch):
    """RELIABILITY FIX regression: a genuinely unexpected exception (not
    one of our domain rejections) must propagate — NOT be swallowed into
    a 200 — so Telegram's own delivery retry actually happens, and
    mark_update_processed() must never be called for it. TestClient's
    default raise_server_exceptions=True means the exception surfaces
    here directly rather than as a response object -- that IS the
    behavior under test (a real deployment turns this into a 5xx, which
    is what makes Telegram redeliver)."""
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    marked = []
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: marked.append(uid))

    def _boom(*a, **kw):
        raise RuntimeError("transient DB hiccup")
    monkeypatch.setattr(telegram_store, "link_via_token", _boom)

    with pytest.raises(RuntimeError, match="transient DB hiccup"):
        client.post(
            "/api/telegram/webhook",
            json={"update_id": 9, "message": {"text": "/start tok", "chat": {"id": 7}, "from": {"id": 7}}},
            headers=WEBHOOK_HEADERS,
        )

    assert marked == []  # never marked processed -- Telegram's retry can still be handled


def test_webhook_malformed_ids_are_ignored_not_500(client, monkeypatch):
    """HARDEN SIGNED WEBHOOK INPUT: a from.id/chat.id that isn't a real
    id (garbage type) must not blow up in a bare int(...) — it should be
    treated as "not a usable /start" and ignored cleanly."""
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    marked = []
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: marked.append(uid))
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    resp = client.post(
        "/api/telegram/webhook",
        json={
            "update_id": 10,
            "message": {"text": "/start tok", "chat": {"id": "not-a-number"}, "from": {"id": [1, 2, 3]}},
        },
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert sent == []  # never reached send_message -- both ids failed validation
    assert marked == [10]


def test_webhook_malformed_message_shape_is_ignored_not_500(client, monkeypatch):
    monkeypatch.setattr(telegram_store, "claim_update", lambda uid: "process")
    marked = []
    monkeypatch.setattr(telegram_store, "mark_update_processed", lambda uid: marked.append(uid))

    resp = client.post(
        "/api/telegram/webhook",
        json={"update_id": 11, "message": "not even a dict"},
        headers=WEBHOOK_HEADERS,
    )

    assert resp.status_code == 200
    assert marked == [11]


# ── POST /api/telegram/unlink ─────────────────────────────────────────────

def test_unlink_requires_authenticated_session(client):
    resp = client.post("/api/telegram/unlink")
    assert resp.status_code == 401


def test_unlink_calls_store(client, monkeypatch):
    user = _login(client, monkeypatch)
    calls = []
    monkeypatch.setattr(telegram_store, "unlink_telegram_account", lambda uid: calls.append(uid))

    resp = client.post("/api/telegram/unlink")

    assert resp.status_code == 200
    assert calls == [user["id"]]


# ── GET /api/telegram/status ─────────────────────────────────────────────

def test_status_requires_authenticated_session(client):
    resp = client.get("/api/telegram/status")
    assert resp.status_code == 401


def test_status_hides_raw_telegram_ids(client, monkeypatch):
    user = make_user(
        telegram_linked=True,
        telegram_user_id=123456789,
        telegram_chat_id=123456789,
        telegram_linked_at=datetime(2030, 1, 1),
    )
    token = "raw-session-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash",
                         lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id",
                         lambda uid: user if uid == user["id"] else None)
    client.cookies.set("sonya_session", token)

    resp = client.get("/api/telegram/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["linked"] is True
    assert body["bot_username"] == "sonya_group_bot"
    assert "linked_at" in body
    assert "telegram_user_id" not in body
    assert "telegram_chat_id" not in body
    # Belt and suspenders: the raw numeric id must not appear anywhere in
    # the serialized response body at all.
    assert "123456789" not in resp.text


def test_status_not_linked(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.get("/api/telegram/status")
    assert resp.status_code == 200
    assert resp.json()["linked"] is False


# ── Secrets never leak into responses ────────────────────────────────────

def test_bot_token_and_webhook_secret_never_appear_in_any_response(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAFakeBotTokenForTestingOnly")
    user = _login(client, monkeypatch)
    monkeypatch.setattr(
        telegram_store, "create_link_token",
        lambda uid: {"token": "raw-token-xyz", "id": "tok-1", "expires_at": datetime(2030, 1, 1)},
    )

    link_resp = client.post("/api/telegram/link-token")
    status_resp = client.get("/api/telegram/status")

    for resp in (link_resp, status_resp):
        assert "123456:AAFakeBotTokenForTestingOnly" not in resp.text
        assert WEBHOOK_SECRET not in resp.text
