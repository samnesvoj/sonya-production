"""
Tests for the passwordless email auth flow.

All PostgreSQL access (scripts.auth_store) and email sending
(scripts.email_sender) are monkeypatched — these are unit/contract tests for
the auth business logic and HTTP layer, not integration tests against a real
Postgres instance. See tests/conftest.py for the env-var setup.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts import auth_store
from scripts.auth_security import hash_code
from tests.conftest import make_auth_code, make_session, make_user


# ── request-code ——————————————————————————————————————————————————————————————

def test_request_code_creates_hashed_auth_code(client, monkeypatch):
    captured = {}

    def fake_create_auth_code(email, code_hash, purpose, expires_at, ip_address=None):
        captured["email"] = email
        captured["code_hash"] = code_hash
        captured["purpose"] = purpose
        captured["expires_at"] = expires_at
        return "code-id-1"

    def fake_get_latest_pending_code(email, purpose):
        return None  # no cooldown collision

    sent = {}

    def fake_send_auth_code_email(to_email, code, purpose):
        sent["to_email"] = to_email
        sent["code"] = code
        sent["purpose"] = purpose

    monkeypatch.setattr(auth_store, "create_auth_code", fake_create_auth_code)
    monkeypatch.setattr(auth_store, "get_latest_pending_code", fake_get_latest_pending_code)
    monkeypatch.setattr("scripts.auth_routes.send_auth_code_email", fake_send_auth_code_email)

    resp = client.post("/api/auth/request-code", json={"email": "New.User@Example.com", "purpose": "register"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # The raw code was sent to the user...
    raw_code = sent["code"]
    assert len(raw_code) == 6 and raw_code.isdigit()

    # ...but only a hash was ever handed to the store, and that hash is
    # NOT the raw code and NOT trivially reversible (looks like sha256 hex).
    assert captured["code_hash"] != raw_code
    assert len(captured["code_hash"]) == 64
    all(c in "0123456789abcdef" for c in captured["code_hash"])

    # The stored hash matches what verify-code would recompute for this
    # exact (email, purpose, code) triple.
    expected_hash = hash_code(raw_code, "new.user@example.com", "register")
    assert captured["code_hash"] == expected_hash
    assert captured["email"] == "new.user@example.com"


def test_request_code_rejects_invalid_email(client):
    resp = client.post("/api/auth/request-code", json={"email": "not-an-email", "purpose": "login"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_email"


def test_request_code_returns_503_when_smtp_not_configured(client, monkeypatch):
    monkeypatch.setattr(auth_store, "create_auth_code", lambda **kw: "id")
    monkeypatch.setattr(auth_store, "get_latest_pending_code", lambda *a, **kw: None)
    # No SMTP env configured in test env -> email_sender raises EmailNotConfiguredError for real.
    resp = client.post("/api/auth/request-code", json={"email": "user@example.com", "purpose": "login"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "email_not_configured"


# ── verify-code ———————————————————————————————————————————————————————————————

def test_verify_code_wrong_code_fails(client, monkeypatch):
    email = "user@example.com"
    purpose = "login"
    correct_hash = hash_code("111111", email, purpose)
    pending = make_auth_code(email, correct_hash, purpose)

    monkeypatch.setattr(auth_store, "get_latest_pending_code", lambda e, p: pending)
    attempts_calls = []
    monkeypatch.setattr(auth_store, "increment_code_attempts", lambda code_id: attempts_calls.append(code_id) or 1)

    resp = client.post("/api/auth/verify-code", json={"email": email, "code": "222222", "purpose": purpose})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_or_expired_code"
    assert attempts_calls == [pending["id"]]


def test_verify_code_expired_fails(client, monkeypatch):
    email = "user@example.com"
    purpose = "login"
    code = "333333"
    code_hash = hash_code(code, email, purpose)
    expired = make_auth_code(
        email, code_hash, purpose,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    monkeypatch.setattr(auth_store, "get_latest_pending_code", lambda e, p: expired)

    resp = client.post("/api/auth/verify-code", json={"email": email, "code": code, "purpose": purpose})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_or_expired_code"


def test_verify_code_max_attempts_exceeded_fails(client, monkeypatch):
    email = "user@example.com"
    purpose = "login"
    code = "444444"
    code_hash = hash_code(code, email, purpose)
    maxed_out = make_auth_code(email, code_hash, purpose, attempts=5)

    monkeypatch.setattr(auth_store, "get_latest_pending_code", lambda e, p: maxed_out)

    resp = client.post("/api/auth/verify-code", json={"email": email, "code": code, "purpose": purpose})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_or_expired_code"


def test_verify_code_valid_creates_user_and_session(client, monkeypatch):
    email = "brandnew@example.com"
    purpose = "register"
    code = "555555"
    code_hash = hash_code(code, email, purpose)
    pending = make_auth_code(email, code_hash, purpose)
    new_user = make_user(email=email)

    monkeypatch.setattr(auth_store, "get_latest_pending_code", lambda e, p: pending)
    monkeypatch.setattr(auth_store, "consume_code", lambda code_id: None)
    monkeypatch.setattr(auth_store, "get_user_by_email", lambda e: None)  # not registered yet
    monkeypatch.setattr(auth_store, "get_or_create_user", lambda e: new_user)
    monkeypatch.setattr(auth_store, "mark_email_verified", lambda uid: None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: new_user)

    created_sessions = []
    monkeypatch.setattr(
        auth_store, "create_session",
        lambda user_id, token_hash, expires_at, user_agent=None, ip_address=None: (
            created_sessions.append((user_id, token_hash)), "session-id",
        )[-1],
    )

    resp = client.post("/api/auth/verify-code", json={"email": email, "code": code, "purpose": purpose})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == new_user["id"]
    assert body["email"] == email
    assert body["plan_type"] == "free"
    assert body["free_video_limit"] == 1
    assert body["free_video_used"] == 0
    assert body["telegram_linked"] is False

    # An opaque session cookie was set — HttpOnly, and never the raw hash.
    assert "sonya_session" in resp.cookies
    raw_token = resp.cookies["sonya_session"]
    assert len(created_sessions) == 1
    assert created_sessions[0][0] == new_user["id"]
    assert created_sessions[0][1] != raw_token  # only the hash was persisted

    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_header
    assert "Secure" in set_cookie_header
    assert "SameSite=lax" in set_cookie_header or "samesite=lax" in set_cookie_header.lower()


# ── /api/auth/me ————————————————————————————————————————————————————————————

def test_auth_me_requires_session(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_auth_me_works_with_valid_cookie(client, monkeypatch):
    user = make_user()
    token = "raw-test-token"
    from scripts.auth_security import hash_session_token
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)

    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)

    client.cookies.set("sonya_session", token)
    resp = client.get("/api/auth/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user["id"]
    assert body["email"] == user["email"]


def test_auth_me_rejects_unknown_session(client, monkeypatch):
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: None)
    client.cookies.set("sonya_session", "garbage-token")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


# ── logout ————————————————————————————————————————————————————————————————————

def test_logout_revokes_session_and_clears_cookie(client, monkeypatch):
    revoked = []
    monkeypatch.setattr(auth_store, "revoke_session_by_token_hash", lambda th: revoked.append(th))

    client.cookies.set("sonya_session", "some-token")
    resp = client.post("/api/auth/logout")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(revoked) == 1

    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "sonya_session=" in set_cookie_header
    # Deleting a cookie is expressed as Max-Age=0 (or an expiry in the past).
    assert "Max-Age=0" in set_cookie_header or "max-age=0" in set_cookie_header.lower()


def test_logout_without_cookie_is_a_no_op_ok(client, monkeypatch):
    revoked = []
    monkeypatch.setattr(auth_store, "revoke_session_by_token_hash", lambda th: revoked.append(th))
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert revoked == []
