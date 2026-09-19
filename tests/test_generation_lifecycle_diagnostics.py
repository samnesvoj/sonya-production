"""
Full-lifecycle diagnostics for the generation pipeline (see
docs/QA_GENERATION_LIFECYCLE.md and scripts/qa/lifecycle_diagnostics.py).

Unlike the other tests/test_generation_jobs_*.py files, which each check
one endpoint/behavior in isolation, this module drives the REAL request
handlers for the entire FILE_SELECTED -> ... -> FRONTEND_RESULT_RECEIVED
chain in one go (upload -> validate -> S3 -> DB -> queued -> worker claim
-> processing -> complete -> result-url), using the same
run_backend_lifecycle() helper the `python scripts/qa/diagnose_generation_flow.py`
CLI uses -- so a developer gets the exact same PASS/FAIL-per-stage report
whether they run it here (`pytest -k lifecycle`) or from the CLI.

Everything is mocked at the same boundary functions the rest of the suite
already mocks (create_job_with_quota, upload_bytes, claim_specific_job,
complete_job, generate_presigned_get_url, ...) -- no real S3, Postgres, or
GPU worker involved anywhere.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from scripts import auth_store
from scripts.auth_security import hash_session_token
from scripts.qa.lifecycle_diagnostics import (
    STAGE_ORDER, all_passed, format_report, probe_async, run_backend_lifecycle,
)
from tests.conftest import make_session, make_user

WORKER_SECRET_ENV = "WORKER_SECRET"


@pytest.fixture()
def logged_in_client(client, monkeypatch):
    """A TestClient with a valid session cookie already set, plus an
    in-memory fake job store wired into the real endpoint handlers (same
    pattern as tests/test_generation_jobs_auth.py and
    tests/test_worker_endpoints.py). Returns (client, probes) -- probes is
    the dict run_backend_lifecycle() reads for fine-grained stage timing.
    """
    user = make_user()
    token = "lifecycle-diagnostic-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)

    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash",
                         lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)

    jobs: dict = {}
    probes: dict = {}

    import scripts.prod_generation_api as api

    def fake_create_job_with_quota(job_id, user_id, mode, params, s3_input_key,
                                    idempotency_key, idempotency_fingerprint, queue_priority=0,
                                    bypass_quota=False):
        row = {"id": job_id, "user_id": user_id, "mode": mode, "status": "queued",
               "created_at": datetime.now(timezone.utc), "s3_output_key": None}
        jobs[job_id] = row
        return {"outcome": "created", "job": row}

    def fake_upload_bytes(content, key, content_type=None):
        import time
        probes["s3_upload_start"] = time.monotonic()
        probes["s3_upload_done"] = time.monotonic()

    monkeypatch.setattr(api, "validate_upload", probe_async(probes, "validated_at", api.validate_upload))
    monkeypatch.setattr(api, "upload_bytes", fake_upload_bytes)
    monkeypatch.setattr(api, "create_job_with_quota", fake_create_job_with_quota)
    monkeypatch.setattr(api, "get_job", lambda job_id: jobs.get(job_id))
    monkeypatch.setattr(api, "add_job_file", lambda **kw: "file-id")
    monkeypatch.setattr(api, "list_job_files", lambda job_id: [])
    monkeypatch.setattr(api, "build_input_key",
                         lambda user_id, job_id, mode, ext: f"users/{user_id}/jobs/{job_id}/{mode}/input/file{ext}")
    monkeypatch.setattr(api, "delete_object", lambda key: True)
    monkeypatch.setattr(api, "update_job_status",
                         lambda job_id, status: jobs.__setitem__(job_id, {**jobs[job_id], "status": status}) if job_id in jobs else None)

    def fake_claim_specific_job(job_id, worker_id):
        job = jobs.get(job_id)
        if not job or job["status"] != "queued":
            return None
        job["status"] = "claimed"
        return dict(job)

    monkeypatch.setattr(api, "claim_specific_job", fake_claim_specific_job)

    def fake_complete_job(job_id, s3_output_key, clip_count=None, processing_ms=None, enrichment_keys=None):
        if job_id in jobs:
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["s3_output_key"] = s3_output_key

    monkeypatch.setattr(api, "complete_job", fake_complete_job)
    monkeypatch.setattr(api, "generate_presigned_get_url",
                         lambda s3_key, expires_in=3600: f"https://s3.example.invalid/{s3_key}?sig=test")
    monkeypatch.setattr("scripts.prod_job_store._get_conn",
                         lambda: (_ for _ in ()).throw(RuntimeError("no DB available in tests")))

    client.cookies.set("sonya_session", token)
    return client, probes


def test_happy_path_every_stage_passes(logged_in_client, monkeypatch):
    """The canonical success run: every stage in STAGE_ORDER reports PASS,
    in order, and the job actually reaches a fetchable result URL -- the
    same walk a real short local video should produce end to end."""
    client, probes = logged_in_client
    monkeypatch.setenv(WORKER_SECRET_ENV, "test-worker-secret-do-not-use-in-production")

    results = run_backend_lifecycle(
        client, mode="virality", probes=probes, stage_timeout_s=5.0,
        simulate_worker=True, worker_secret="test-worker-secret-do-not-use-in-production",
    )

    print(format_report(results))
    assert [r.stage for r in results] == STAGE_ORDER
    assert all_passed(results), format_report(results)


def test_backend_400_invalid_mode_fails_before_upload_validation(logged_in_client):
    """A request with a mode the backend doesn't recognize must be rejected
    before validate_upload ever runs (mode is checked first in
    create_generation_job) -- the diagnostic must attribute this to
    BACKEND_REQUEST_RECEIVED, not to a bogus INPUT_VALIDATED failure."""
    client, probes = logged_in_client

    results = run_backend_lifecycle(client, mode="not-a-real-mode", probes=probes, simulate_worker=False)

    by_stage = {r.stage: r for r in results}
    assert by_stage["CLIENT_UPLOAD_COMPLETE"].ok, "the HTTP round trip itself succeeded (got a real 400)"
    assert not by_stage["BACKEND_REQUEST_RECEIVED"].ok
    assert "invalid_mode" in by_stage["BACKEND_REQUEST_RECEIVED"].detail
    assert not by_stage["INPUT_VALIDATED"].ok and by_stage["INPUT_VALIDATED"].detail == "not reached"
    assert "validated_at" not in probes, "validate_upload must never be called for a rejected mode"


def test_backend_5xx_during_s3_upload_is_attributed_to_s3_stage(logged_in_client, monkeypatch):
    """If upload validation succeeds but the S3 PUT itself raises, the
    report must say S3_INPUT_UPLOAD failed -- not misattribute it to
    INPUT_VALIDATED (which did succeed) or silently report a generic
    failure with no stage information."""
    client, probes = logged_in_client
    import scripts.prod_generation_api as api

    def raising_upload_bytes(content, key, content_type=None):
        probes["s3_upload_start"] = __import__("time").monotonic()
        raise RuntimeError("simulated S3 outage")

    monkeypatch.setattr(api, "upload_bytes", raising_upload_bytes)

    results = run_backend_lifecycle(client, mode="virality", probes=probes, simulate_worker=False)

    by_stage = {r.stage: r for r in results}
    assert by_stage["INPUT_VALIDATED"].ok
    assert not by_stage["S3_INPUT_UPLOAD"].ok
    assert "500" in by_stage["S3_INPUT_UPLOAD"].detail
    assert not by_stage["JOB_CREATED"].ok and by_stage["JOB_CREATED"].detail == "not reached"


def test_stalled_s3_upload_is_reported_as_timeout_not_a_hang(logged_in_client, monkeypatch):
    """Characterizes the exact production symptom under controlled
    conditions: S3 never returns. Proves the DIAGNOSTIC tool itself has a
    hard per-stage deadline and reports TIMEOUT promptly -- it must never
    hang the test suite waiting for a dependency that never responds,
    mirroring how "Создаём задачу" can hang for a real user today because
    *no* layer (browser fetch, this endpoint, boto3 call site) enforces one.
    """
    client, probes = logged_in_client
    import scripts.prod_generation_api as api

    def never_returns_upload_bytes(content, key, content_type=None):
        import time
        probes["s3_upload_start"] = time.monotonic()
        # Long enough to clearly outlast stage_timeout_s below (0.5s) so this
        # is unambiguously a stall, not a slow-but-real response. Bounded
        # (not truly infinite) so the test's own TestClient fixture teardown
        # -- which must wait for this thread since it blocks the ASGI
        # event-loop thread directly, see lifecycle_diagnostics._call_with_deadline --
        # finishes in a couple of seconds instead of hanging the suite.
        time.sleep(2)

    monkeypatch.setattr(api, "upload_bytes", never_returns_upload_bytes)

    results = run_backend_lifecycle(client, mode="virality", probes=probes,
                                     stage_timeout_s=0.5, simulate_worker=False)

    by_stage = {r.stage: r for r in results}
    assert by_stage["CLIENT_UPLOAD_START"].ok
    assert not by_stage["CLIENT_UPLOAD_COMPLETE"].ok
    assert by_stage["CLIENT_UPLOAD_COMPLETE"].timed_out
    assert by_stage["CLIENT_UPLOAD_COMPLETE"].duration_s < 2.0, "must report TIMEOUT near stage_timeout_s, not wait out the 5s stall"
    assert not by_stage["BACKEND_REQUEST_RECEIVED"].ok


def test_job_created_but_never_claimed_stays_visibly_queued(logged_in_client):
    """A job that's created successfully but never picked up by a worker
    (the P0-1 vast.ai claim bug in docs/SONYA_AUDIT.md is exactly this
    shape) must show QUEUED as the last PASS with every later stage marked
    "not reached" -- never silently reported as success."""
    client, probes = logged_in_client

    results = run_backend_lifecycle(client, mode="virality", probes=probes, simulate_worker=False)

    by_stage = {r.stage: r for r in results}
    assert by_stage["JOB_CREATED"].ok
    assert by_stage["QUEUED"].ok
    assert by_stage["WORKER_CLAIMED"].detail == "not reached"
