"""
Free-plan generation limit -- backend never returned 402 FREE_PLAN_USED
before this change (grep FREE_PLAN_USED **/*.py was 0 hits), even though
the frontend (auth.js::checkAndCreateVideoJob) already expected it. This
covers both the pure _is_pro_active() logic and the actual 402 raised from
POST /api/generation/jobs.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from scripts import auth_store
from scripts.auth_security import hash_session_token
from scripts.prod_generation_api import _is_pro_active
from tests.conftest import make_session, make_user


def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftyp" + b"\x00" * 64


def _login(client, monkeypatch, user):
    token = "raw-session-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)
    client.cookies.set("sonya_session", token)


# ── _is_pro_active ────────────────────────────────────────────────────────

def test_is_pro_active_true_for_active_unexpired_pro():
    user = make_user(plan_type="pro", plan_status="active",
                      plan_active_until=datetime.now(timezone.utc) + timedelta(days=10))
    assert _is_pro_active(user) is True


def test_is_pro_active_false_for_free_plan():
    user = make_user(plan_type="free")
    assert _is_pro_active(user) is False


def test_is_pro_active_false_for_expired_pro():
    """The core regression: plan_type stays "pro" after expiry (nothing
    resets it), so plan_active_until must be checked too."""
    user = make_user(plan_type="pro", plan_status="active",
                      plan_active_until=datetime.now(timezone.utc) - timedelta(days=1))
    assert _is_pro_active(user) is False


def test_is_pro_active_false_for_pro_type_but_inactive_status():
    user = make_user(plan_type="pro", plan_status="cancelled",
                      plan_active_until=datetime.now(timezone.utc) + timedelta(days=10))
    assert _is_pro_active(user) is False


# ── POST /api/generation/jobs gate ───────────────────────────────────────

def test_free_plan_limit_reached_returns_402(client, monkeypatch):
    user = make_user(plan_type="free", free_video_limit=1, free_video_used=1)
    _login(client, monkeypatch, user)

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "FREE_PLAN_USED"


def test_active_pro_bypasses_free_limit(client, monkeypatch):
    user = make_user(plan_type="pro", plan_status="active",
                      plan_active_until=datetime.now(timezone.utc) + timedelta(days=10),
                      free_video_limit=1, free_video_used=5)
    _login(client, monkeypatch, user)

    captured_bypass = {}

    def fake_create_job_with_quota(**kw):
        captured_bypass["bypass_quota"] = kw["bypass_quota"]
        return {"outcome": "created", "job": {"id": "job-1", "user_id": user["id"], "mode": "virality", "status": "queued", "created_at": None}}

    monkeypatch.setattr("scripts.prod_generation_api.create_job_with_quota", fake_create_job_with_quota)
    monkeypatch.setattr("scripts.prod_generation_api.get_job", lambda job_id: {"created_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr("scripts.prod_generation_api.add_job_file", lambda **kw: "file-id")
    monkeypatch.setattr("scripts.prod_generation_api.upload_bytes", lambda content, key, content_type=None: None)
    monkeypatch.setattr(
        "scripts.prod_generation_api.build_input_key",
        lambda user_id, job_id, mode, ext: f"users/{user_id}/jobs/{job_id}/{mode}/input/file{ext}",
    )

    def _no_db_conn():
        raise RuntimeError("no DB available in tests")
    monkeypatch.setattr("scripts.prod_job_store._get_conn", _no_db_conn)

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
    )
    assert resp.status_code == 202, resp.text
    assert captured_bypass["bypass_quota"] is True


def test_expired_pro_does_not_bypass_free_limit(client, monkeypatch):
    """The regression this whole gate exists for: an expired Pro user
    (plan_type still "pro") must NOT get unlimited free generations."""
    user = make_user(plan_type="pro", plan_status="active",
                      plan_active_until=datetime.now(timezone.utc) - timedelta(days=1),
                      free_video_limit=1, free_video_used=1)
    _login(client, monkeypatch, user)

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "FREE_PLAN_USED"
