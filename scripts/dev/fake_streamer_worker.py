"""
scripts/dev/fake_streamer_worker.py
====================================
NO-GPU stand-in for scripts/gpu_worker.py, used ONLY against
scripts/dev/local_e2e_server.py. Never imported by production code, never
runs ffmpeg, never touches vast.ai or real GPU/render infrastructure.

Talks to the REAL worker-facing HTTP endpoints exactly like the real
gpu_worker.py's "api" backend mode does (POST /api/worker/claim, .../
complete, .../fail, and streamer's own .../analysis-result) -- the only
thing "fake" here is that no video is actually analyzed or rendered:
analyze always returns a fixed set of test segments, and compose always
"produces" the same tiny local ffmpeg-generated fixture clip (scripts/dev/
fixtures/tiny_clip.mp4), written straight into local_fake_s3 the same way
local_e2e_server.py's patched upload_bytes() would have written it.

Direct Postgres reads (via scripts.prod_job_store) are used ONLY to look
up which job_id/batch_id to act on next -- convenience for this CLI, not
part of the data path. Every actual STATE CHANGE (segments persisted,
jobs completed/failed) goes through the same HTTP worker contract a real
GPU worker uses.

Usage:
    export DATABASE_URL="postgresql://$(whoami)@localhost/sonya_test"
    python3 scripts/dev/fake_streamer_worker.py analyze --batch-id <id>
    python3 scripts/dev/fake_streamer_worker.py compose-all --batch-id <id> [--fail-last | --fail-all]
    python3 scripts/dev/fake_streamer_worker.py status --batch-id <id>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", f"postgresql://{os.environ.get('USER', 'postgres')}@localhost/sonya_test")
# Must match scripts/dev/local_e2e_server.py's own default exactly -- this
# is a SEPARATE process authenticating to that server's worker endpoints,
# not a shared-memory monkeypatch.
WORKER_SECRET = os.environ.get("WORKER_SECRET", "local-e2e-worker-secret-do-not-use-in-production")
API_BASE = os.environ.get("SONYA_LOCAL_E2E_API_BASE", f"http://127.0.0.1:{os.environ.get('SONYA_LOCAL_E2E_PORT', '8811')}")

from scripts import prod_job_store as store  # noqa: E402
from scripts.dev import local_fake_s3  # noqa: E402
from scripts.prod_s3_storage import build_output_key  # noqa: E402

FIXTURE_CLIP = REPO_ROOT / "scripts" / "dev" / "fixtures" / "tiny_clip.mp4"

AUTH = {"Authorization": f"Bearer {WORKER_SECRET}"}

DEFAULT_TEST_SEGMENTS: List[Dict[str, Any]] = [
    {"start_sec": 120, "duration_sec": 42, "title": "Первый сильный момент",
     "description": "Реакция стримера на неожиданный клатч", "score": 0.91, "recommended": True},
    {"start_sec": 340, "duration_sec": 35, "title": "Смешной момент с чатом",
     "description": "Стример читает донат в прямом эфире", "score": 0.74, "recommended": False},
    {"start_sec": 610, "duration_sec": 58, "title": "Напряжённый раунд",
     "description": "Долгий клатч на классе", "score": 0.83, "recommended": True},
    {"start_sec": 900, "duration_sec": 29, "title": "Забавная фраза",
     "description": "Мем-момент, потенциально виральный", "score": 0.62, "recommended": False},
    {"start_sec": 1500, "duration_sec": 47, "title": "Финальный камбэк",
     "description": "Победа в решающем раунде", "score": 0.95, "recommended": True},
]


def _post(path: str, json: dict, timeout: int = 15) -> requests.Response:
    resp = requests.post(f"{API_BASE}{path}", json=json, headers=AUTH, timeout=timeout)
    return resp


def _claim(job_id: str, worker_id: str = "fake-streamer-worker") -> Optional[dict]:
    resp = _post("/api/worker/claim", {"worker_id": worker_id, "job_id": job_id})
    resp.raise_for_status()
    return resp.json().get("job")


def fake_analyze(batch_id: str, segments: Optional[List[Dict[str, Any]]] = None) -> None:
    batch = store.get_streamer_batch_by_id(batch_id)
    if not batch:
        raise SystemExit(f"no such batch: {batch_id}")
    job_id = batch.get("analysis_job_id")
    if not job_id:
        raise SystemExit(f"batch {batch_id} has no analysis_job_id yet -- ingest may still be running")

    claimed = _claim(job_id)
    if not claimed:
        print(f"[fake_worker] analyze job {job_id} not claimable (already claimed/terminal?) -- continuing anyway")

    segments = segments if segments is not None else DEFAULT_TEST_SEGMENTS
    resp = _post(
        f"/api/worker/streamer/batches/{batch_id}/analysis-result",
        {"segments": segments, "crop_hints": {}, "warnings": [],
         "webcam_boxes_found": 1, "active_speaker_segs": 1},
    )
    resp.raise_for_status()
    print(f"[fake_worker] analysis-result submitted batch_id={batch_id} segments={len(segments)}")

    manifest_key = build_output_key(user_id=batch["user_id"], job_id=job_id, mode="streamer", filename="analysis.json")
    local_fake_s3.write_bytes(manifest_key, b'{"fake": true}')
    complete = _post(f"/api/worker/jobs/{job_id}/complete", {
        "s3_output_key": manifest_key, "clip_count": len(segments), "processing_ms": 500,
    })
    complete.raise_for_status()
    print(f"[fake_worker] analyze job {job_id} completed -> batch should now be awaiting_selection")


def fake_compose_one(job_id: str, fail: bool = False) -> None:
    job = store.get_job(job_id)
    if not job:
        raise SystemExit(f"no such job: {job_id}")

    claimed = _claim(job_id)
    if not claimed:
        print(f"[fake_worker] compose job {job_id} not claimable -- continuing anyway")

    if fail:
        resp = _post(f"/api/worker/jobs/{job_id}/fail", {
            "error_code": "FAKE_COMPOSE_FAILURE", "error_message": "simulated failure (NO-GPU harness)",
            "retry": False,
        })
        resp.raise_for_status()
        print(f"[fake_worker] compose job {job_id} FAILED (simulated)")
        return

    out_key = build_output_key(user_id=job["user_id"], job_id=job_id, mode="streamer", filename="clip.mp4")
    local_fake_s3.write_file(out_key, str(FIXTURE_CLIP))
    resp = _post(f"/api/worker/jobs/{job_id}/complete", {
        "s3_output_key": out_key, "clip_count": 1, "processing_ms": 300,
    })
    resp.raise_for_status()
    print(f"[fake_worker] compose job {job_id} completed -> {out_key}")


def fake_compose_all(batch_id: str, fail_last: bool = False, fail_all: bool = False) -> None:
    clip_jobs = store.list_streamer_clip_jobs(batch_id)
    if not clip_jobs:
        raise SystemExit(f"batch {batch_id} has no compose jobs yet -- confirm a selection first")

    for i, cj in enumerate(clip_jobs):
        is_last = i == len(clip_jobs) - 1
        fail = fail_all or (fail_last and is_last)
        fake_compose_one(cj["job_id"], fail=fail)


def show_status(batch_id: str) -> None:
    batch = store.get_streamer_batch_by_id(batch_id)
    if not batch:
        raise SystemExit(f"no such batch: {batch_id}")
    print(f"batch {batch_id}: status={batch['status']} analysis_job_id={batch.get('analysis_job_id')} error={batch.get('error')}")
    for cj in store.list_streamer_clip_jobs(batch_id):
        job = store.get_job(cj["job_id"])
        print(f"  segment={cj['segment_id']} job={cj['job_id']} status={job['status'] if job else '?'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_analyze = sub.add_parser("analyze", help="fake-complete a batch's analysis phase with test segments")
    p_analyze.add_argument("--batch-id", required=True)

    p_compose = sub.add_parser("compose", help="fake-complete (or fail) ONE compose job")
    p_compose.add_argument("--job-id", required=True)
    p_compose.add_argument("--fail", action="store_true")

    p_compose_all = sub.add_parser("compose-all", help="fake-complete every compose job attached to a batch")
    p_compose_all.add_argument("--batch-id", required=True)
    p_compose_all.add_argument("--fail-last", action="store_true", help="scenario B: partially_failed")
    p_compose_all.add_argument("--fail-all", action="store_true", help="scenario C: failed")

    p_status = sub.add_parser("status", help="print a batch's current status + its compose jobs")
    p_status.add_argument("--batch-id", required=True)

    args = parser.parse_args()
    if args.cmd == "analyze":
        fake_analyze(args.batch_id)
    elif args.cmd == "compose":
        fake_compose_one(args.job_id, fail=args.fail)
    elif args.cmd == "compose-all":
        fake_compose_all(args.batch_id, fail_last=args.fail_last, fail_all=args.fail_all)
    elif args.cmd == "status":
        show_status(args.batch_id)


if __name__ == "__main__":
    main()
