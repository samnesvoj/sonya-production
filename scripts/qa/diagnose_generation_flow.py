#!/usr/bin/env python3
"""
diagnose_generation_flow.py
============================
One reproducible command to answer "where did SONYA get stuck this time?"
without manually submitting a video and guessing.

    python scripts/qa/diagnose_generation_flow.py

Default (and only currently-implemented) mode is --mode mock: boots the
real FastAPI app (scripts.prod_generation_api) in-process via
TestClient, with the S3 and Postgres boundary functions replaced by
in-memory fakes (same functions the existing pytest suite mocks -- see
tests/test_generation_jobs_auth.py, tests/test_worker_endpoints.py). No
network, no real S3 bucket, no real Postgres, no vast.ai, no cost. It
walks the ENTIRE lifecycle -- upload -> validate -> S3 -> DB -> queued ->
worker claim -> processing -> complete -> result-url -- through the real
request-handling code paths (not reimplemented logic), and prints one
PASS/FAIL/TIMEOUT line per stage.

Exit code is 0 iff every stage PASSed.

--scenario lets you reproduce the specific failure modes QA asked for:
    happy       full success path (default)
    bad-mode    backend 400 (invalid mode) at INPUT_VALIDATED
    s3-down     backend 500 (S3 upload raises) at S3_INPUT_UPLOAD
    stalled-s3  S3 upload never returns -- proves the tool itself times
                out cleanly (stage_timeout_s) instead of hanging forever,
                exactly the "Создаём задачу forever" symptom under
                controlled conditions

--live BASE_URL is scaffolded (see --help) for pointing this at a real
deployment with a real small test video, but is intentionally NOT wired
to actually run here -- per SONYA QA policy this session must not spend
real GPU/S3 cost or touch production without explicit approval. Fill in
the TODO in _run_live() with real session-cookie bootstrapping before
using it, and start with a throwaway/staging account.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Must be set before scripts.prod_generation_api is imported (module-level
# CORS/env checks) -- mirrors /conftest.py's setdefault() pattern exactly.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("CORS_ORIGINS", "https://testserver")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret-do-not-use-in-production")
os.environ.setdefault("WORKER_SECRET", "qa-diagnostic-worker-secret")

from scripts.qa.lifecycle_diagnostics import (  # noqa: E402
    format_report, all_passed, probe_async, run_backend_lifecycle,
)


def _make_user() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid.uuid4()), "email": "qa-diagnostic@example.com",
        "email_verified_at": now, "plan_type": "free", "plan_status": "active",
        "plan_active_until": None, "free_video_limit": 1000, "free_video_used": 0,
        "telegram_linked": False, "created_at": now, "updated_at": now,
    }


def _make_session(user_id: str, token_hash: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid.uuid4()), "user_id": user_id, "token_hash": token_hash,
        "expires_at": now + timedelta(days=30), "revoked_at": None,
        "user_agent": "sonya-qa-diagnostic", "ip_address": "127.0.0.1", "created_at": now,
    }


def _run_mock(scenario: str, stage_timeout_s: float) -> int:
    from fastapi.testclient import TestClient
    from scripts import auth_store
    from scripts.auth_security import hash_session_token
    import scripts.prod_generation_api as api

    user = _make_user()
    token = "qa-diagnostic-session-token"
    token_hash = hash_session_token(token)
    session = _make_session(user["id"], token_hash)

    jobs: dict = {}
    probes: dict = {}

    def fake_create_job_idempotent(job_id, user_id, mode, params, s3_input_key,
                                    idempotency_key, idempotency_fingerprint, queue_priority=0):
        row = {"id": job_id, "user_id": user_id, "mode": mode, "status": "queued",
               "created_at": datetime.now(timezone.utc), "s3_output_key": None}
        jobs[job_id] = row
        return row

    def fake_get_job(job_id):
        return jobs.get(job_id)

    def fake_update_job_status(job_id, status):
        if job_id in jobs:
            jobs[job_id]["status"] = status

    def fake_claim_specific_job(job_id, worker_id):
        job = jobs.get(job_id)
        if not job or job["status"] != "queued":
            return None
        job["status"] = "claimed"
        return dict(job)

    def fake_complete_job(job_id, s3_output_key, clip_count=None, processing_ms=None, enrichment_keys=None):
        if job_id in jobs:
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["s3_output_key"] = s3_output_key

    def fake_generate_presigned_get_url(s3_key, expires_in=3600):
        return f"https://s3.example.invalid/{s3_key}?sig=qa-diagnostic"

    # A genuinely infinite sleep here would also freeze the TestClient's
    # own in-process ASGI event-loop thread (upload_bytes is called
    # synchronously from inside the async endpoint, so it blocks that
    # thread directly, not just the calling side) -- run_backend_lifecycle's
    # daemon-thread deadline still lets THIS function return and report
    # TIMEOUT/FAIL promptly, but the `with TestClient(...)` block's own
    # teardown afterward would then hang waiting for that frozen thread.
    # 15s is long enough to clearly exceed any reasonable --stage-timeout
    # (default 5s; the CLI's own stalled-s3 walkthrough below uses 1s) while
    # staying short enough that the process still exits promptly overall.
    upload_delay = 15.0 if scenario == "stalled-s3" else 0.0
    upload_error = RuntimeError("simulated S3 outage") if scenario == "s3-down" else None

    def timed_upload_bytes(content, key, content_type=None):
        probes["s3_upload_start"] = __import__("time").monotonic()
        if upload_delay:
            import time as _t
            _t.sleep(upload_delay)
        if upload_error:
            raise upload_error
        probes["s3_upload_done"] = __import__("time").monotonic()

    patches = [
        mock.patch.object(auth_store, "get_active_session_by_token_hash",
                           lambda th: session if th == token_hash else None),
        mock.patch.object(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None),
        mock.patch.object(api, "validate_upload", probe_async(probes, "validated_at", api.validate_upload)),
        mock.patch.object(api, "create_job_idempotent", fake_create_job_idempotent),
        mock.patch.object(api, "get_job", fake_get_job),
        mock.patch.object(api, "add_job_file", lambda **kw: "file-id"),
        mock.patch.object(api, "list_job_files", lambda job_id: []),
        mock.patch.object(api, "build_input_key",
                           lambda user_id, job_id, mode, ext: f"users/{user_id}/jobs/{job_id}/{mode}/input/file{ext}"),
        mock.patch.object(api, "upload_bytes", timed_upload_bytes),
        mock.patch.object(api, "delete_object", lambda key: True),
        mock.patch.object(api, "update_job_status", fake_update_job_status),
        mock.patch.object(api, "claim_specific_job", fake_claim_specific_job),
        mock.patch.object(api, "complete_job", fake_complete_job),
        mock.patch.object(api, "generate_presigned_get_url", fake_generate_presigned_get_url),
        mock.patch("scripts.prod_job_store._get_conn",
                    lambda: (_ for _ in ()).throw(RuntimeError("no DB available in QA mock mode"))),
    ]
    for p in patches:
        p.start()
    try:
        with TestClient(api.app, base_url="https://testserver") as client:
            client.cookies.set("sonya_session", token)
            mode = "does-not-exist" if scenario == "bad-mode" else "virality"
            results = run_backend_lifecycle(
                client, mode=mode, probes=probes, stage_timeout_s=stage_timeout_s,
                simulate_worker=True, worker_secret=os.environ["WORKER_SECRET"],
            )
    finally:
        for p in reversed(patches):
            p.stop()

    print(format_report(results))
    return 0 if all_passed(results) else 1


def _run_live(base_url: str, stage_timeout_s: float) -> int:
    """NOT implemented in this session -- scaffolded only.

    Before this can safely run against a real (staging/prod) deployment it
    needs, at minimum:
      1. A dedicated QA/staging account -- never a real user's session.
      2. Real cookie-based login (POST /auth/request-code + /auth/verify-code
         with a test mailbox this script can read the code from), since
         run_backend_lifecycle() here expects an already-authenticated
         `client` (cookies.set(...) in mock mode) and there is no
         S3/Postgres to monkeypatch on a real server.
      3. `probes` will stay empty in live mode -- INPUT_VALIDATED /
         S3_INPUT_UPLOAD collapse into CLIENT_UPLOAD_COMPLETE's single
         span, same as a real browser sees. That's intentional, not a bug
         to fix here.
      4. simulate_worker=False -- do not call /api/worker/* against a real
         deployment from a QA script; let the real dispatcher/worker claim
         it, and poll GET /api/generation/jobs/{id} for QUEUED /
         WORKER_CLAIMED / PROCESSING / JOB_COMPLETED instead (poll, don't
         drive).
      5. An explicit, small, cheap test video and an explicit user
         confirmation gate before it runs (this hits a real GPU worker).
    """
    raise NotImplementedError("see _run_live() docstring")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--scenario", choices=["happy", "bad-mode", "s3-down", "stalled-s3"], default="happy",
                         help="Only meaningful with --mode mock.")
    parser.add_argument("--stage-timeout", type=float, default=5.0,
                         help="Per-stage timeout in seconds (stalled-s3 uses this to prove the tool doesn't hang).")
    parser.add_argument("--live-base-url", default=None,
                         help="Reserved for --mode live; not implemented in this session, see module docstring.")
    args = parser.parse_args()

    if args.mode == "live":
        print("`--mode live` is scaffolded but intentionally not implemented in this session:\n"
              "SONYA QA policy for this session prohibits real GPU/S3 cost or touching production\n"
              "without explicit approval. See the _run_live() TODO in this file, and\n"
              "docs/QA_GENERATION_LIFECYCLE.md for what it needs to do before it's safe to run.",
              file=sys.stderr)
        return 2

    return _run_mock(args.scenario, args.stage_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
