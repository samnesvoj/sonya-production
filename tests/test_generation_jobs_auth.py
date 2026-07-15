"""
Generation job endpoints must resolve identity from the sonya_session
cookie, never from a client-supplied X-User-Id header.
"""
from __future__ import annotations

import io

from scripts import auth_store
from scripts.auth_security import hash_session_token
from tests.conftest import make_session, make_user


def _mp4_bytes() -> bytes:
    # Minimal bytes that pass upload_security's magic-byte sniff for mp4.
    return b"\x00\x00\x00\x18ftyp" + b"\x00" * 64


def test_create_job_requires_authenticated_session(client):
    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
    )
    assert resp.status_code == 401


def test_x_user_id_header_alone_is_not_trusted(client):
    """A browser-facing endpoint must ignore X-User-Id entirely."""
    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"X-User-Id": "attacker-supplied-id"},
    )
    assert resp.status_code == 401


def test_create_job_succeeds_with_valid_session_cookie(client, monkeypatch):
    user = make_user()
    token = "raw-session-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)

    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)

    created_jobs = {}

    def fake_create_job(job_id, user_id, mode, params, s3_input_key, queue_priority=0):
        created_jobs["job_id"] = job_id
        created_jobs["user_id"] = user_id
        created_jobs["mode"] = mode
        created_jobs["queue_priority"] = queue_priority
        return job_id

    monkeypatch.setattr("scripts.prod_generation_api.create_job", fake_create_job)
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

    client.cookies.set("sonya_session", token)
    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["mode"] == "virality"

    # The job was created for the *authenticated* user, not any client-supplied id.
    assert created_jobs["user_id"] == user["id"]
    # Priority was resolved from the user's plan_type in Postgres (free -> 100),
    # never from a client header.
    assert created_jobs["queue_priority"] == 100


def test_list_jobs_requires_session(client):
    resp = client.get("/api/generation/jobs")
    assert resp.status_code == 401


def test_get_job_by_id_requires_session(client):
    resp = client.get("/api/generation/jobs/some-job-id")
    assert resp.status_code == 401
