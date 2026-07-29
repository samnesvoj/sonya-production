"""
Idempotency-Key tests for POST /api/generation/jobs (P0 follow-up).

Pure logic (key validation, fingerprint canonicalization) is tested
directly, no DB/HTTP involved. Endpoint-level tests use the TestClient
with prod_job_store.create_job_idempotent/get_job_by_idempotency_key and
prod_s3_storage.delete_object monkeypatched -- no real Postgres or S3 call.
See tests/test_job_idempotency_postgres.py for the real-Postgres
concurrent-insert test that this file deliberately does NOT attempt to
fake (a mocked test cannot prove the unique index actually serializes
concurrent requests).
"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException

from scripts import auth_store
from scripts.auth_security import hash_session_token
from scripts.prod_generation_api import _compute_idempotency_fingerprint, _validate_idempotency_key
from tests.conftest import make_session, make_user


def _mp4_bytes(marker: bytes = b"") -> bytes:
    # Minimal bytes that pass upload_security's magic-byte sniff for mp4;
    # `marker` lets tests produce two distinguishable "different files".
    return b"\x00\x00\x00\x18ftyp" + marker + b"\x00" * (64 - len(marker))


# ── _validate_idempotency_key ────────────────────────────────────────────────


def test_missing_header_is_legacy_none():
    assert _validate_idempotency_key(None, "trace-1") is None


def test_empty_after_trim_is_400():
    with pytest.raises(HTTPException) as exc:
        _validate_idempotency_key("   ", "trace-1")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "invalid_idempotency_key"


def test_literal_empty_string_is_400():
    with pytest.raises(HTTPException) as exc:
        _validate_idempotency_key("", "trace-1")
    assert exc.value.status_code == 400


def test_too_long_is_400():
    with pytest.raises(HTTPException) as exc:
        _validate_idempotency_key("a" * 256, "trace-1")
    assert exc.value.status_code == 400


def test_invalid_characters_are_400():
    with pytest.raises(HTTPException) as exc:
        _validate_idempotency_key("has a space", "trace-1")
    assert exc.value.status_code == 400


def test_valid_key_is_trimmed_and_returned():
    assert _validate_idempotency_key("  abc-123_DEF.4:5  ", "trace-1") == "abc-123_DEF.4:5"


def test_max_length_key_is_accepted():
    key = "a" * 255
    assert _validate_idempotency_key(key, "trace-1") == key


# ── _compute_idempotency_fingerprint ─────────────────────────────────────────

_PARAMS_A = {
    "source": {"mode": "upload", "url": "", "fileName": "a.mp4", "platform": "upload"},
    "brand": "sonya",
    "clipType": "viral",
    "settings": {"subtitleStyle": "viral", "voiceoverEnabled": True, "voiceType": "male-1", "translation": "none"},
    "frontend": "legacy_static_sonya",
    "production_endpoint": "/api/generation/jobs",
}


def test_fingerprint_stable_across_json_key_order():
    reordered = {
        "production_endpoint": "/api/generation/jobs",
        "clipType": "viral",
        "settings": {"translation": "none", "voiceType": "male-1", "voiceoverEnabled": True, "subtitleStyle": "viral"},
        "brand": "sonya",
        "frontend": "legacy_static_sonya",
        "source": {"platform": "upload", "fileName": "a.mp4", "url": "", "mode": "upload"},
    }
    fp1 = _compute_idempotency_fingerprint("virality", b"filebytes", _PARAMS_A)
    fp2 = _compute_idempotency_fingerprint("virality", b"filebytes", reordered)
    assert fp1 == fp2


def test_fingerprint_ignores_frontend_and_production_endpoint_and_filename():
    other = dict(_PARAMS_A, frontend="some_other_version", production_endpoint="/other")
    other["source"] = dict(_PARAMS_A["source"], fileName="completely-different-name.mov")
    fp1 = _compute_idempotency_fingerprint("virality", b"filebytes", _PARAMS_A)
    fp2 = _compute_idempotency_fingerprint("virality", b"filebytes", other)
    assert fp1 == fp2


def test_fingerprint_changes_with_different_mode():
    fp1 = _compute_idempotency_fingerprint("virality", b"filebytes", _PARAMS_A)
    fp2 = _compute_idempotency_fingerprint("stories", b"filebytes", _PARAMS_A)
    assert fp1 != fp2


def test_fingerprint_changes_with_different_file_content():
    fp1 = _compute_idempotency_fingerprint("virality", b"filebytes-A", _PARAMS_A)
    fp2 = _compute_idempotency_fingerprint("virality", b"filebytes-B", _PARAMS_A)
    assert fp1 != fp2


def test_fingerprint_changes_with_unknown_extra_field():
    """A field the backend doesn't explicitly know about must still count --
    no manual allowlist to fall out of sync."""
    extended = dict(_PARAMS_A, some_future_field={"nested": "value"})
    fp1 = _compute_idempotency_fingerprint("virality", b"filebytes", _PARAMS_A)
    fp2 = _compute_idempotency_fingerprint("virality", b"filebytes", extended)
    assert fp1 != fp2


def test_fingerprint_distinguishes_missing_empty_and_present_values():
    params_missing = {"settings": {}}
    params_empty = {"settings": {"translation": ""}}
    params_none = {"settings": {"translation": None}}
    params_value = {"settings": {"translation": "en"}}

    fps = {
        _compute_idempotency_fingerprint("virality", b"x", params_missing),
        _compute_idempotency_fingerprint("virality", b"x", params_empty),
        _compute_idempotency_fingerprint("virality", b"x", params_none),
        _compute_idempotency_fingerprint("virality", b"x", params_value),
    }
    assert len(fps) == 4, "missing/empty-string/None/value must all fingerprint differently"


# ── Endpoint-level: conflict / replay / 409 / cleanup ────────────────────────


def _login(monkeypatch, client):
    user = make_user()
    token = "raw-session-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)
    client.cookies.set("sonya_session", token)
    return user


def _wire_common(monkeypatch, *, s3_keys):
    monkeypatch.setattr("scripts.prod_generation_api.upload_bytes", lambda content, key, content_type=None: s3_keys.append(key))
    monkeypatch.setattr(
        "scripts.prod_generation_api.build_input_key",
        lambda user_id, job_id, mode, ext: f"users/{user_id}/jobs/{job_id}/{mode}/input/file{ext}",
    )
    monkeypatch.setattr("scripts.prod_generation_api.get_job", lambda job_id: {"created_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr("scripts.prod_generation_api.add_job_file", lambda **kw: "file-id")

    def _no_db_conn():
        raise RuntimeError("no DB available in tests")
    monkeypatch.setattr("scripts.prod_job_store._get_conn", _no_db_conn)


def test_empty_idempotency_key_header_is_400(client, monkeypatch):
    _login(monkeypatch, client)
    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"Idempotency-Key": "   "},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_idempotency_key"


def test_too_long_idempotency_key_header_is_400(client, monkeypatch):
    _login(monkeypatch, client)
    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"Idempotency-Key": "x" * 300},
    )
    assert resp.status_code == 400


def test_new_job_returns_202_without_replay_header(client, monkeypatch):
    _login(monkeypatch, client)
    s3_keys = []
    _wire_common(monkeypatch, s3_keys=s3_keys)
    monkeypatch.setattr(
        "scripts.prod_generation_api.create_job_idempotent",
        lambda **kw: {"id": "job-new", "user_id": kw["user_id"], "mode": kw["mode"], "status": "queued"},
    )
    delete_calls = []
    monkeypatch.setattr("scripts.prod_generation_api.delete_object", lambda key, bucket=None: delete_calls.append(key))

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"Idempotency-Key": "abc-123"},
    )

    assert resp.status_code == 202
    assert "Idempotency-Replayed" not in resp.headers
    assert resp.json()["job_id"] == "job-new"
    assert delete_calls == []  # nothing orphaned -- this was the winning insert


def test_replay_same_fingerprint_returns_existing_job_with_replayed_header(client, monkeypatch):
    user = _login(monkeypatch, client)
    s3_keys = []
    _wire_common(monkeypatch, s3_keys=s3_keys)

    # Conflict: create_job_idempotent reports "already exists"
    monkeypatch.setattr("scripts.prod_generation_api.create_job_idempotent", lambda **kw: None)

    existing_job = {
        "id": "job-existing", "user_id": user["id"], "mode": "virality", "status": "queued",
        "created_at": "2026-01-01T00:00:00Z",
    }

    captured_fp = {}

    def fake_get_existing(user_id, key):
        return existing_job

    monkeypatch.setattr("scripts.prod_generation_api.get_job_by_idempotency_key", fake_get_existing)

    # Patch the fingerprint computer to a fixed value so we can make the
    # "existing" row's stored fingerprint match it exactly (replay path).
    monkeypatch.setattr("scripts.prod_generation_api._compute_idempotency_fingerprint", lambda *a, **kw: "fp-match")
    existing_job["idempotency_fingerprint"] = "fp-match"

    delete_calls = []
    monkeypatch.setattr("scripts.prod_generation_api.delete_object", lambda key, bucket=None: delete_calls.append(key))

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"Idempotency-Key": "same-key"},
    )

    assert resp.status_code == 202
    assert resp.headers.get("Idempotency-Replayed") == "true"
    assert resp.json()["job_id"] == "job-existing"
    # The orphaned upload from THIS losing request was cleaned up -- and
    # only that one; the existing job's own s3_input_key was never touched.
    assert len(delete_calls) == 1
    assert delete_calls[0] == s3_keys[0]
    assert delete_calls[0] != existing_job.get("s3_input_key")


def test_different_fingerprint_same_key_is_409_and_cleans_up_orphan(client, monkeypatch):
    user = _login(monkeypatch, client)
    s3_keys = []
    _wire_common(monkeypatch, s3_keys=s3_keys)

    monkeypatch.setattr("scripts.prod_generation_api.create_job_idempotent", lambda **kw: None)
    existing_job = {
        "id": "job-existing", "user_id": user["id"], "mode": "virality", "status": "queued",
        "idempotency_fingerprint": "fp-original",
    }
    monkeypatch.setattr("scripts.prod_generation_api.get_job_by_idempotency_key", lambda user_id, key: existing_job)
    monkeypatch.setattr("scripts.prod_generation_api._compute_idempotency_fingerprint", lambda *a, **kw: "fp-different")

    delete_calls = []
    monkeypatch.setattr("scripts.prod_generation_api.delete_object", lambda key, bucket=None: delete_calls.append(key))

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"Idempotency-Key": "same-key"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "idempotency_key_conflict"
    assert len(delete_calls) == 1
    assert delete_calls[0] == s3_keys[0]


def test_db_error_after_upload_cleans_up_orphan_and_returns_500(client, monkeypatch):
    _login(monkeypatch, client)
    s3_keys = []
    _wire_common(monkeypatch, s3_keys=s3_keys)

    def _boom(**kw):
        raise RuntimeError("db exploded")
    monkeypatch.setattr("scripts.prod_generation_api.create_job_idempotent", _boom)

    delete_calls = []
    monkeypatch.setattr("scripts.prod_generation_api.delete_object", lambda key, bucket=None: delete_calls.append(key))

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
        headers={"Idempotency-Key": "abc-123"},
    )

    assert resp.status_code == 500
    assert len(delete_calls) == 1
    assert delete_calls[0] == s3_keys[0]


def test_missing_idempotency_key_is_legacy_behavior(client, monkeypatch):
    """No header at all -> always a new job, no fingerprint computed."""
    _login(monkeypatch, client)
    s3_keys = []
    _wire_common(monkeypatch, s3_keys=s3_keys)

    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"id": "job-legacy", "user_id": kw["user_id"], "mode": kw["mode"], "status": "queued"}

    monkeypatch.setattr("scripts.prod_generation_api.create_job_idempotent", fake_create)

    resp = client.post(
        "/api/generation/jobs",
        data={"mode": "virality"},
        files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")},
    )

    assert resp.status_code == 202
    assert "Idempotency-Replayed" not in resp.headers
    assert captured["idempotency_key"] is None
    assert captured["idempotency_fingerprint"] is None
