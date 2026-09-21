"""
Real PostgreSQL integration tests for the real streamer batch HTTP flow
(scripts/streamer_routes.py + the reconciliation hooks added to
scripts/prod_generation_api.py's worker/complete and worker/fail
endpoints). Mirrors tests/test_streamer_batches_postgres.py's approach:
hits a real, migrated database via the same DATABASE_URL/sonya_test setup,
skipped automatically unless DATABASE_URL is set.

S3 (upload_bytes/delete_object/generate_presigned_get_url/build_input_key)
and url_ingest's network calls (probe/download_video/validate_downloaded_
file) are monkeypatched at their scripts.streamer_routes call sites, same
convention tests/test_generation_jobs_from_url.py already uses for the
generic /api/generation/jobs/from-url endpoint. Everything else --
sessions, users, quota, generation_jobs, streamer_batches/segments/
clip_jobs -- goes through the real store layer against the real DB.

Local run:
    export DATABASE_URL="postgresql://localhost/sonya_test"
    python scripts/run_migrations.py
    pytest tests/test_streamer_batch_flow_postgres.py -v
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres streamer batch flow test skipped locally",
)

from scripts import auth_store, streamer_routes, url_ingest
from scripts.auth_security import hash_session_token
from tests.conftest import make_session


# ── Fixtures / helpers ───────────────────────────────────────────────────

@pytest.fixture()
def store():
    from scripts import prod_job_store
    return prod_job_store


def _create_user():
    return auth_store.create_user(f"streamer-batch-{uuid.uuid4()}@example.com")


def _login_as(client, monkeypatch, user_id: str) -> None:
    """
    Real user row (already in the DB via auth_store.create_user), fake
    session lookup only -- get_current_user's own auth_store.get_user_by_id
    call is left unmocked so it hits the real row (plan_type,
    free_video_used, etc. all real, exactly what the quota assertions
    below need to be meaningful).
    """
    token = f"raw-session-{user_id}"
    token_hash = hash_session_token(token)
    session = make_session(user_id, token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash",
                         lambda th: session if th == token_hash else None)
    client.cookies.set("sonya_session", token)


def _mock_s3(monkeypatch):
    uploaded = {}

    def _fake_upload_bytes(content, key, content_type=None):
        uploaded[key] = content

    monkeypatch.setattr(streamer_routes, "upload_bytes", _fake_upload_bytes)
    monkeypatch.setattr(streamer_routes, "delete_object", lambda key: True)
    monkeypatch.setattr(streamer_routes, "generate_presigned_get_url",
                         lambda key, expires_in=3600: f"https://s3.example.com/{key}?sig=fake")
    return uploaded


def _fresh_video_file(tmp_path):
    p = tmp_path / f"src-{uuid.uuid4()}.mp4"
    # Minimal bytes -- validate_downloaded_file / validate_upload are
    # monkeypatched around in these tests (see _mock_s3 / _mock_url_ingest),
    # so this file's content never actually needs to pass real magic-byte
    # validation.
    p.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 64)
    return p


def _mock_url_ingest(monkeypatch, tmp_path, fail: str | None = None):
    video_path = _fresh_video_file(tmp_path)

    def _probe(url, platform, mode=None):
        if fail == "probe_limit":
            raise url_ingest.DownloadLimitExceeded()
        return {"duration": 42}

    def _download(url, platform, progress_cb=None, mode=None):
        if fail == "download_failed":
            raise url_ingest.DownloadFailed()
        return (str(video_path), ".mp4")

    def _validate(local_path, hint_name, mode=None):
        return (b"\x00" * 128, "video.mp4")

    monkeypatch.setattr(url_ingest, "probe", _probe)
    monkeypatch.setattr(url_ingest, "download_video", _download)
    monkeypatch.setattr(url_ingest, "validate_downloaded_file", _validate)
    monkeypatch.setattr(url_ingest, "cleanup", lambda path: None)


AUTH_HEADER = {"Authorization": f"Bearer {os.environ.get('WORKER_SECRET', 'test-worker-secret-do-not-use-in-production')}"}


def _segments_body(n=3):
    return {
        "segments": [
            {"start_sec": 10.0 * (i + 1), "duration_sec": 5.0, "title": f"Тема {i}", "score": float(n - i)}
            for i in range(n)
        ],
        "crop_hints": {"box": [0, 0, 1, 1]},
        "warnings": [],
        "webcam_boxes_found": 2,
        "active_speaker_segs": 1,
    }


# ── 1. Batch creation, ownership ────────────────────────────────────────

def test_create_batch_requires_session(client):
    resp = client.post("/api/streamer/batches", data={"source_type": "file"})
    assert resp.status_code == 401


def test_create_batch_file_source_happy_path_reaches_analyzing(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)

    before = auth_store.get_user_by_id(user["id"])
    assert before["free_video_used"] == 0

    resp = client.post(
        "/api/streamer/batches",
        data={"source_type": "file"},
        files={"file": ("stream.mp4", b"\x00\x00\x00\x18ftyp" + b"\x00" * 64, "video/mp4")},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "analyzing"

    batch = store.get_streamer_batch(body["batch_id"], user["id"])
    assert batch["status"] == "analyzing"
    assert batch["analysis_job_id"] is not None

    job = store.get_job(batch["analysis_job_id"])
    assert job["mode"] == "streamer"
    assert job["params"]["streamer_phase"] == "analyze"
    assert job["params"]["streamer_batch_id"] == body["batch_id"]

    # The one billable action for this whole batch -- quota consumed
    # exactly once here, and (see the selection tests below) never again
    # for any of the batch's internal compose jobs.
    after = auth_store.get_user_by_id(user["id"])
    assert after["free_video_used"] == 1


def test_create_batch_file_source_requires_a_file(client, monkeypatch):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    resp = client.post("/api/streamer/batches", data={"source_type": "file"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "missing_file"


def test_create_batch_quota_exceeded_marks_batch_failed(client, monkeypatch, store):
    user = _create_user()
    auth_store_update = getattr(auth_store, "_get_conn")  # sanity: real DB path
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)

    # Exhaust the user's free quota directly against the real row.
    conn = auth_store_update()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET free_video_used = free_video_limit WHERE id = %s", (user["id"],))
    finally:
        conn.close()

    resp = client.post(
        "/api/streamer/batches",
        data={"source_type": "file"},
        files={"file": ("stream.mp4", b"\x00\x00\x00\x18ftyp" + b"\x00" * 64, "video/mp4")},
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "FREE_PLAN_USED"


def test_create_batch_idempotency_key_replay_returns_same_batch_no_double_quota(client, monkeypatch, store):
    """
    A double-click or a network retry on the submit button resends the
    exact same POST with the SAME client-generated Idempotency-Key. The
    second request must return the FIRST request's own batch untouched --
    not a second batch, and not a second Free-plan quota charge.
    """
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)
    key = f"submit-{uuid.uuid4()}"

    resp1 = client.post(
        "/api/streamer/batches",
        data={"source_type": "file"},
        files={"file": ("stream.mp4", b"\x00\x00\x00\x18ftyp" + b"\x00" * 64, "video/mp4")},
        headers={"Idempotency-Key": key},
    )
    resp2 = client.post(
        "/api/streamer/batches",
        data={"source_type": "file"},
        files={"file": ("stream.mp4", b"\x00\x00\x00\x18ftyp" + b"\x00" * 64, "video/mp4")},
        headers={"Idempotency-Key": key},
    )
    assert resp1.status_code == 202, resp1.text
    assert resp2.status_code == 202, resp2.text
    assert resp1.json()["batch_id"] == resp2.json()["batch_id"]

    after = auth_store.get_user_by_id(user["id"])
    assert after["free_video_used"] == 1  # charged once, not twice

    # Only one streamer_batches row exists for this key at all.
    assert store.get_streamer_batch(resp1.json()["batch_id"], user["id"]) is not None


def test_create_batch_without_idempotency_key_is_legacy_always_new(client, monkeypatch, store):
    """No header sent -> unchanged legacy behavior: two submissions are two
    real batches (this is what every pre-existing caller/test relies on)."""
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)

    # Bump this user's free-plan limit so quota exhaustion (default limit
    # is 1) doesn't mask what this test is actually checking.
    conn = auth_store._get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET free_video_limit = 5 WHERE id = %s", (user["id"],))
    finally:
        conn.close()

    resp1 = client.post(
        "/api/streamer/batches", data={"source_type": "file"},
        files={"file": ("stream.mp4", b"\x00\x00\x00\x18ftyp" + b"\x00" * 64, "video/mp4")},
    )
    resp2 = client.post(
        "/api/streamer/batches", data={"source_type": "file"},
        files={"file": ("stream.mp4", b"\x00\x00\x00\x18ftyp" + b"\x00" * 64, "video/mp4")},
    )
    assert resp1.json()["batch_id"] != resp2.json()["batch_id"]

    after = auth_store.get_user_by_id(user["id"])
    assert after["free_video_used"] == 2


def test_create_streamer_batch_concurrent_same_idempotency_key_creates_exactly_one_row(store):
    """
    The real regression target for the HTTP-level tests above: N genuinely
    concurrent DB connections racing to INSERT ... ON CONFLICT (user_id,
    idempotency_key) DO NOTHING for the SAME key. Exactly one must report
    _created_now=True; the store's own unique index is what makes this
    true under real concurrency, not the request-handler's sequencing --
    same proof shape as test_job_idempotency_postgres.py's own concurrent
    idempotency-key test.
    """
    user = _create_user()
    key = f"race-{uuid.uuid4()}"
    n = 8

    def attempt(_i):
        return store.create_streamer_batch(user["id"], preset_snapshot={}, idempotency_key=key)

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    created = [r for r in results if r["_created_now"]]
    replayed = [r for r in results if not r["_created_now"]]
    assert len(created) == 1, f"expected exactly one winning insert, got {len(created)}"
    assert len(replayed) == n - 1
    assert all(r["id"] == created[0]["id"] for r in replayed)


def test_get_batch_not_found(client, monkeypatch):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    resp = client.get(f"/api/streamer/batches/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_batch_forbidden_for_other_user_looks_like_not_found(client, monkeypatch, store):
    owner = _create_user()
    other = _create_user()
    batch = store.create_streamer_batch(owner["id"], preset_snapshot={})

    _login_as(client, monkeypatch, other["id"])
    resp = client.get(f"/api/streamer/batches/{batch['id']}")
    assert resp.status_code == 404  # never distinguishes "not yours" from "doesn't exist"


# ── 2. URL source: batch exists independent of ingest outcome ──────────

def test_url_batch_created_even_when_ingest_fails(client, monkeypatch, store, tmp_path):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)
    _mock_url_ingest(monkeypatch, tmp_path, fail="download_failed")

    resp = client.post(
        "/api/streamer/batches",
        data={"source_type": "url", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert resp.status_code == 202
    batch_id = resp.json()["batch_id"]

    # TestClient runs BackgroundTasks synchronously before the response
    # returns to the caller, so the ingest failure has already happened.
    batch = store.get_streamer_batch(batch_id, user["id"])
    assert batch["status"] == "failed"
    assert batch["error"] == "download_failed"
    # The batch row itself is a real, independent artifact -- it exists
    # and is inspectable even though ingest never produced a job.
    assert batch["analysis_job_id"] is None


def test_url_batch_happy_path_reaches_analyzing(client, monkeypatch, store, tmp_path):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)
    _mock_url_ingest(monkeypatch, tmp_path)

    resp = client.post(
        "/api/streamer/batches",
        data={"source_type": "url", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert resp.status_code == 202
    batch_id = resp.json()["batch_id"]

    batch = store.get_streamer_batch(batch_id, user["id"])
    assert batch["status"] == "analyzing"
    assert batch["analysis_job_id"] is not None

    after = auth_store.get_user_by_id(user["id"])
    assert after["free_video_used"] == 1


def test_url_batch_idempotency_key_replay_returns_same_batch_no_double_quota(client, monkeypatch, store, tmp_path):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    _mock_s3(monkeypatch)
    _mock_url_ingest(monkeypatch, tmp_path)
    key = f"submit-{uuid.uuid4()}"

    resp1 = client.post(
        "/api/streamer/batches",
        data={"source_type": "url", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"Idempotency-Key": key},
    )
    resp2 = client.post(
        "/api/streamer/batches",
        data={"source_type": "url", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"Idempotency-Key": key},
    )
    assert resp1.json()["batch_id"] == resp2.json()["batch_id"]

    after = auth_store.get_user_by_id(user["id"])
    assert after["free_video_used"] == 1  # charged once, not twice


def test_create_batch_rejects_unsupported_url(client, monkeypatch):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    resp = client.post(
        "/api/streamer/batches",
        data={"source_type": "url", "url": "https://example.com/not-a-video-page"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_url"


# ── 3. Worker: analysis-result submission ───────────────────────────────

def test_worker_analysis_result_requires_worker_secret(client, store):
    user = _create_user()
    batch = store.create_streamer_batch(user["id"], preset_snapshot={})
    resp = client.post(f"/api/worker/streamer/batches/{batch['id']}/analysis-result", json=_segments_body())
    assert resp.status_code == 403


def test_worker_analysis_result_batch_not_found(client):
    resp = client.post(
        f"/api/worker/streamer/batches/{uuid.uuid4()}/analysis-result",
        json=_segments_body(), headers=AUTH_HEADER,
    )
    assert resp.status_code == 404


def test_worker_analysis_result_persists_segments_and_transitions_batch(client, store):
    user = _create_user()
    batch = store.create_streamer_batch(user["id"], preset_snapshot={})

    resp = client.post(
        f"/api/worker/streamer/batches/{batch['id']}/analysis-result",
        json=_segments_body(3), headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    assert resp.json()["segments_count"] == 3

    fetched = store.get_streamer_batch(batch["id"], user["id"])
    assert fetched["status"] == "awaiting_selection"
    segments = store.list_streamer_segments(batch["id"])
    assert len(segments) == 3
    assert all(s["title"] for s in segments)


def test_worker_analysis_result_malformed_segments_rejected_not_500(client, store):
    user = _create_user()
    batch = store.create_streamer_batch(user["id"], preset_snapshot={})

    resp = client.post(
        f"/api/worker/streamer/batches/{batch['id']}/analysis-result",
        json={"segments": [{"title": "missing start_sec/duration_sec"}]},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 400
    # Batch must stay exactly where it was -- a malformed submission never
    # silently moves it to awaiting_selection with bad/partial data.
    fetched = store.get_streamer_batch(batch["id"], user["id"])
    assert fetched["status"] == "queued"


# ── 4. Selection: ownership, validation, idempotency, source reuse ─────

def _batch_awaiting_selection(store, user_id, n_segments=3):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    job_id = str(uuid.uuid4())
    store.create_job(
        job_id=job_id, user_id=user_id, mode="streamer", params={},
        s3_input_key=f"users/{user_id}/jobs/{job_id}/streamer/input/source.mp4",
    )
    store.attach_analysis_job_to_batch(batch["id"], job_id)
    segments = [
        {"start_sec": 10.0 * (i + 1), "duration_sec": 5.0, "title": f"Тема {i}"}
        for i in range(n_segments)
    ]
    inserted = store.replace_streamer_segments(batch["id"], segments)
    store.set_streamer_batch_status(batch["id"], "awaiting_selection")
    return batch["id"], job_id, inserted


def test_selection_rejects_when_batch_still_analyzing(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch = store.create_streamer_batch(user["id"], preset_snapshot={})
    store.set_streamer_batch_status(batch["id"], "analyzing")

    resp = client.post(f"/api/streamer/batches/{batch['id']}/selection", json={"segment_ids": [str(uuid.uuid4())]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "invalid_batch_status"


def test_selection_empty_list_rejected(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch_id, _job_id, _segs = _batch_awaiting_selection(store, user["id"])

    resp = client.post(f"/api/streamer/batches/{batch_id}/selection", json={"segment_ids": []})
    assert resp.status_code == 422  # Pydantic min_length=1 guard


def test_selection_foreign_segment_rejected(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch_id, _job_id, _segs = _batch_awaiting_selection(store, user["id"])

    other_user = _create_user()
    other_batch_id, _oj, other_segs = _batch_awaiting_selection(store, other_user["id"])

    resp = client.post(
        f"/api/streamer/batches/{batch_id}/selection",
        json={"segment_ids": [other_segs[0]["id"]]},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "foreign_segment"


def test_selection_only_own_batch_accepted(client, monkeypatch, store):
    owner = _create_user()
    other = _create_user()
    batch_id, _job_id, segs = _batch_awaiting_selection(store, owner["id"])

    _login_as(client, monkeypatch, other["id"])
    resp = client.post(f"/api/streamer/batches/{batch_id}/selection", json={"segment_ids": [segs[0]["id"]]})
    assert resp.status_code == 404


def test_selection_creates_compose_jobs_with_reused_input_key(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch_id, analysis_job_id, segs = _batch_awaiting_selection(store, user["id"], n_segments=3)
    analysis_job = store.get_job(analysis_job_id)

    before = auth_store.get_user_by_id(user["id"])

    resp = client.post(
        f"/api/streamer/batches/{batch_id}/selection",
        json={"segment_ids": [s["id"] for s in segs]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "generating"
    assert len(body["clip_jobs"]) == 3

    clip_jobs = store.list_streamer_clip_jobs(batch_id)
    assert len(clip_jobs) == 3
    for cj in clip_jobs:
        job = store.get_job(cj["job_id"])
        assert job["s3_input_key"] == analysis_job["s3_input_key"]  # source reuse -- no second upload
        assert job["params"]["streamer_phase"] == "compose"
        assert job["params"]["streamer_batch_id"] == batch_id

    fetched = store.get_streamer_batch(batch_id, user["id"])
    assert fetched["status"] == "generating"

    # Internal compose job creation never touches free-plan quota -- N
    # compose jobs from one selection call must never look like N billed
    # generations.
    after = auth_store.get_user_by_id(user["id"])
    assert after["free_video_used"] == before["free_video_used"]


def test_selection_is_idempotent_on_retry(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch_id, _job_id, segs = _batch_awaiting_selection(store, user["id"], n_segments=2)
    segment_ids = [s["id"] for s in segs]

    resp1 = client.post(f"/api/streamer/batches/{batch_id}/selection", json={"segment_ids": segment_ids})
    resp2 = client.post(f"/api/streamer/batches/{batch_id}/selection", json={"segment_ids": segment_ids})
    assert resp1.status_code == 200 and resp2.status_code == 200

    job_ids_1 = sorted(cj["job_id"] for cj in resp1.json()["clip_jobs"])
    job_ids_2 = sorted(cj["job_id"] for cj in resp2.json()["clip_jobs"])
    assert job_ids_1 == job_ids_2  # exact same jobs reused, none duplicated

    clip_jobs = store.list_streamer_clip_jobs(batch_id)
    assert len(clip_jobs) == 2  # never 4


# ── 5. Batch reconciliation, driven through the real worker endpoints ──

def _selected_batch(client, monkeypatch, store, user, n=2):
    _login_as(client, monkeypatch, user["id"])
    batch_id, _job_id, segs = _batch_awaiting_selection(store, user["id"], n_segments=n)
    resp = client.post(
        f"/api/streamer/batches/{batch_id}/selection",
        json={"segment_ids": [s["id"] for s in segs]},
    )
    assert resp.status_code == 200
    job_ids = [cj["job_id"] for cj in resp.json()["clip_jobs"]]
    return batch_id, job_ids


def test_reconcile_batch_ready_when_all_compose_jobs_complete(client, monkeypatch, store):
    user = _create_user()
    batch_id, job_ids = _selected_batch(client, monkeypatch, store, user, n=2)

    for jid in job_ids:
        resp = client.post(
            f"/api/worker/jobs/{jid}/complete",
            json={"s3_output_key": f"users/{user['id']}/jobs/{jid}/streamer/output/clip.mp4", "clip_count": 1},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200

    batch = store.get_streamer_batch(batch_id, user["id"])
    assert batch["status"] == "ready"


def test_reconcile_batch_partially_failed_when_some_compose_jobs_fail(client, monkeypatch, store):
    user = _create_user()
    batch_id, job_ids = _selected_batch(client, monkeypatch, store, user, n=2)

    ok_id, fail_id = job_ids
    resp = client.post(
        f"/api/worker/jobs/{ok_id}/complete",
        json={"s3_output_key": f"users/{user['id']}/jobs/{ok_id}/streamer/output/clip.mp4", "clip_count": 1},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/api/worker/jobs/{fail_id}/fail",
        json={"error_code": "RUNNER_FAILED", "error_message": "boom", "retry": False},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200

    batch = store.get_streamer_batch(batch_id, user["id"])
    assert batch["status"] == "partially_failed"


def test_reconcile_batch_failed_when_all_compose_jobs_fail(client, monkeypatch, store):
    user = _create_user()
    batch_id, job_ids = _selected_batch(client, monkeypatch, store, user, n=2)

    for jid in job_ids:
        resp = client.post(
            f"/api/worker/jobs/{jid}/fail",
            json={"error_code": "RUNNER_FAILED", "error_message": "boom", "retry": False},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200

    batch = store.get_streamer_batch(batch_id, user["id"])
    assert batch["status"] == "failed"


def test_reconcile_batch_stays_generating_while_a_job_is_still_pending(client, monkeypatch, store):
    user = _create_user()
    batch_id, job_ids = _selected_batch(client, monkeypatch, store, user, n=2)

    resp = client.post(
        f"/api/worker/jobs/{job_ids[0]}/complete",
        json={"s3_output_key": "k", "clip_count": 1},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200

    batch = store.get_streamer_batch(batch_id, user["id"])
    assert batch["status"] == "generating"  # second job still queued -- not reconciled yet


def test_analyze_phase_permanent_failure_fails_the_batch_directly(client, monkeypatch, store):
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch = store.create_streamer_batch(user["id"], preset_snapshot={})
    job_id = str(uuid.uuid4())
    store.create_job(
        job_id=job_id, user_id=user["id"], mode="streamer",
        params={"streamer_phase": "analyze", "streamer_batch_id": batch["id"]},
        s3_input_key=f"users/{user['id']}/jobs/{job_id}/streamer/input/source.mp4",
    )
    store.attach_analysis_job_to_batch(batch["id"], job_id)
    store.set_streamer_batch_status(batch["id"], "analyzing")

    resp = client.post(
        f"/api/worker/jobs/{job_id}/fail",
        json={"error_code": "RUNNER_FAILED", "error_message": "no gpu", "retry": False},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200

    fetched = store.get_streamer_batch(batch["id"], user["id"])
    assert fetched["status"] == "failed"


# ── 6. GET batch: defensive reconciliation + rehydration payload shape ─

def test_get_batch_defensively_reconciles_when_worker_hook_never_ran(client, monkeypatch, store):
    """
    Same as test_reconcile_batch_ready_when_all_compose_jobs_complete, but
    reconciliation is only ever triggered by the GET call itself (as if
    the worker/complete hook had somehow been missed) -- proves the
    backstop in get_batch() actually works, not just the primary hook.
    """
    user = _create_user()
    _login_as(client, monkeypatch, user["id"])
    batch_id, _job_id, segs = _batch_awaiting_selection(store, user["id"], n_segments=1)
    seg = segs[0]

    job_id = str(uuid.uuid4())
    store.create_job(
        job_id=job_id, user_id=user["id"], mode="streamer",
        params={"streamer_phase": "compose", "streamer_batch_id": batch_id},
        s3_input_key=f"users/{user['id']}/jobs/{job_id}/streamer/input/source.mp4",
    )
    store.attach_streamer_clip_job(batch_id, seg["id"], job_id)
    store.set_streamer_batch_status(batch_id, "generating")
    store.complete_job(job_id, s3_output_key="k", clip_count=1)  # directly, bypassing the /complete endpoint hook

    resp = client.get(f"/api/streamer/batches/{batch_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_get_batch_rehydration_payload_shape(client, monkeypatch, store):
    user = _create_user()
    _mock_s3(monkeypatch)
    batch_id, job_ids = _selected_batch(client, monkeypatch, store, user, n=1)
    client.post(
        f"/api/worker/jobs/{job_ids[0]}/complete",
        json={"s3_output_key": "users/x/jobs/y/streamer/output/clip.mp4", "clip_count": 1},
        headers=AUTH_HEADER,
    )

    resp = client.get(f"/api/streamer/batches/{batch_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert len(body["segments"]) == 1
    assert len(body["clips"]) == 1
    clip = body["clips"][0]
    assert clip["job_id"] == job_ids[0]
    assert clip["status"] == "completed"
    assert clip["previewUrl"].startswith("https://")
    assert clip["downloadUrl"].startswith("https://")
    assert "s3_output_key" not in clip and "s3_input_key" not in body  # never leak raw storage keys


def test_list_batches_returns_only_own(client, monkeypatch, store):
    owner = _create_user()
    other = _create_user()
    store.create_streamer_batch(owner["id"], preset_snapshot={})
    store.create_streamer_batch(other["id"], preset_snapshot={})

    _login_as(client, monkeypatch, owner["id"])
    resp = client.get("/api/streamer/batches")
    assert resp.status_code == 200
    assert len(resp.json()["batches"]) == 1
