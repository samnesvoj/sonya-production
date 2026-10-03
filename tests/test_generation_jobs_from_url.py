"""
POST /api/generation/jobs/from-url + GET .../from-url/{ingest_id}

Mirrors tests/test_generation_jobs_auth.py's patterns for the file-upload
endpoint: session-cookie auth is required, and once a job is created it
must have gone through the same S3-upload / create_job_idempotent /
add_job_file sequence a file upload uses. url_ingest's actual network
calls (probe/download_video) are monkeypatched — these tests exercise the
API/orchestration code, not yt-dlp or the network.
"""
from __future__ import annotations

from scripts import auth_store, url_ingest
from scripts.auth_security import hash_session_token
from tests.conftest import make_session, make_user


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


def _mock_pipeline(monkeypatch, tmp_path, created_jobs):
    """Stub out everything downstream of a successful download."""
    video_path = tmp_path / "downloaded.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 64)

    monkeypatch.setattr(url_ingest, "probe", lambda url, platform, mode=None, max_duration_sec=None: {"duration": 42})
    monkeypatch.setattr(url_ingest, "download_video",
                         lambda url, platform, progress_cb=None, mode=None: (str(video_path), ".mp4"))
    monkeypatch.setattr(url_ingest, "cleanup", lambda path: None)

    def fake_create_job_with_quota(job_id, user_id, mode, params, s3_input_key,
                                    idempotency_key, idempotency_fingerprint, queue_priority=0,
                                    bypass_quota=False, subscription_id=None):
        created_jobs["job_id"] = job_id
        created_jobs["user_id"] = user_id
        created_jobs["mode"] = mode
        created_jobs["s3_input_key"] = s3_input_key
        return {"outcome": "created", "job": {"id": job_id, "user_id": user_id, "mode": mode, "status": "queued"}}

    monkeypatch.setattr("scripts.prod_generation_api.create_job_with_quota", fake_create_job_with_quota)
    monkeypatch.setattr("scripts.prod_generation_api.add_job_file", lambda **kw: "file-id")
    monkeypatch.setattr("scripts.prod_generation_api.upload_bytes",
                         lambda content, key, content_type=None: None)
    monkeypatch.setattr(
        "scripts.prod_generation_api.build_input_key",
        lambda user_id, job_id, mode, ext: f"users/{user_id}/jobs/{job_id}/{mode}/input/file{ext}",
    )
    monkeypatch.setattr("scripts.prod_job_store._get_conn",
                         lambda: (_ for _ in ()).throw(RuntimeError("no DB available in tests")))


def test_from_url_requires_authenticated_session(client):
    resp = client.post("/api/generation/jobs/from-url",
                        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "virality"})
    assert resp.status_code == 401


def test_from_url_rejects_unsupported_platform(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.post("/api/generation/jobs/from-url",
                        json={"url": "https://example.com/not-a-video-page", "mode": "virality"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_url"


def test_from_url_rejects_ssrf_target(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.post("/api/generation/jobs/from-url",
                        json={"url": "http://169.254.169.254/latest/meta-data/", "mode": "virality"})
    # Path ends without a recognized video extension -> caught by
    # detect_platform() as unsupported before the SSRF check even runs;
    # either rejection is correct, both keep the request from proceeding.
    assert resp.status_code == 400


def test_from_url_rejects_ssrf_target_with_video_extension(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.post("/api/generation/jobs/from-url",
                        json={"url": "http://169.254.169.254/secret.mp4", "mode": "virality"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsafe_url"


def test_from_url_rejects_invalid_mode(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.post("/api/generation/jobs/from-url",
                        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "not_a_mode"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_mode"


def test_from_url_full_success_flow(client, monkeypatch, tmp_path):
    user = _login(client, monkeypatch)
    created_jobs = {}
    _mock_pipeline(monkeypatch, tmp_path, created_jobs)

    start = client.post("/api/generation/jobs/from-url",
                         json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "virality"})
    assert start.status_code == 202
    body = start.json()
    assert body["status"] == "checking"
    assert body["platform"] == "youtube"
    ingest_id = body["ingest_id"]

    # By the time TestClient's .post() returns, the background task (which
    # runs synchronously under TestClient) has already completed.
    status_resp = client.get(f"/api/generation/jobs/from-url/{ingest_id}")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["status"] == "queued"
    assert status_body["job_id"] == created_jobs["job_id"]
    assert created_jobs["user_id"] == user["id"]
    assert created_jobs["mode"] == "virality"


def test_from_url_download_failure_reports_failed_status(client, monkeypatch):
    _login(client, monkeypatch)
    monkeypatch.setattr(url_ingest, "probe", lambda url, platform, mode=None, max_duration_sec=None: {"duration": 10})

    def _boom(url, platform, progress_cb=None, mode=None):
        raise url_ingest.DownloadFailed("network unreachable")
    monkeypatch.setattr(url_ingest, "download_video", _boom)

    start = client.post("/api/generation/jobs/from-url",
                         json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "virality"})
    assert start.status_code == 202
    ingest_id = start.json()["ingest_id"]

    status_resp = client.get(f"/api/generation/jobs/from-url/{ingest_id}")
    body = status_resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "download_failed"


def test_from_url_duration_limit_reports_failed_status(client, monkeypatch):
    _login(client, monkeypatch)

    def _too_long(url, platform, mode=None, max_duration_sec=None):
        raise url_ingest.DownloadLimitExceeded("Видео слишком длинное (120 мин). Максимум — 60 мин.")
    monkeypatch.setattr(url_ingest, "probe", _too_long)

    start = client.post("/api/generation/jobs/from-url",
                         json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "virality"})
    ingest_id = start.json()["ingest_id"]

    status_resp = client.get(f"/api/generation/jobs/from-url/{ingest_id}")
    body = status_resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "limit_exceeded"
    assert "60 мин" in body["message"]


def test_ingest_status_unknown_id_is_404(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.get("/api/generation/jobs/from-url/does-not-exist")
    assert resp.status_code == 404


def test_ingest_status_not_visible_to_other_user(client, monkeypatch, tmp_path):
    _login(client, monkeypatch)
    created_jobs = {}
    _mock_pipeline(monkeypatch, tmp_path, created_jobs)

    start = client.post("/api/generation/jobs/from-url",
                         json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "virality"})
    ingest_id = start.json()["ingest_id"]

    # A second, different authenticated user must not be able to poll the
    # first user's ingest_id.
    other_user = make_user()
    other_token = "other-raw-session-token"
    other_hash = hash_session_token(other_token)
    other_session = make_session(other_user["id"], other_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash",
                         lambda th: other_session if th == other_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id",
                         lambda uid: other_user if uid == other_user["id"] else None)
    client.cookies.set("sonya_session", other_token)

    resp = client.get(f"/api/generation/jobs/from-url/{ingest_id}")
    assert resp.status_code == 404
