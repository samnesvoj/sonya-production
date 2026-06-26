"""
gpu_worker.py
=============
Full-featured GPU worker for SONYA generation pipeline.

Usage:
    python scripts/gpu_worker.py --once --job-id JOB_ID
    python scripts/gpu_worker.py --poll
    python scripts/gpu_worker.py --poll --worker-id my-gpu-node-1

Flow:
    1. claim_job (specific or next pending)
    2. update status → processing
    3. download input from S3
    4. ensure_models_for_mode (model_downloader)
    5. run mode via mode_registry
    6. upload all output files to S3
    7. add_job_file for each output
    8. complete_job (or fail_job on error)
    9. cleanup temp dir
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.mode_registry import get_runner
from scripts.model_downloader import download_models_for_mode
from scripts.prod_job_store import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_CLAIMED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_DOWNLOADING,
    JOB_STATUS_MODEL_DOWNLOADING,
    JOB_STATUS_MODE_RUNNING,
    JOB_STATUS_UPLOADING_RESULT,
    add_job_file,
    claim_next_pending_job,
    claim_specific_job,
    complete_job,
    fail_job,
    get_job,
    requeue_stale_jobs,
    update_job_status,
)
from scripts.prod_s3_storage import (
    build_output_key,
    download_file,
    object_exists,
    upload_file,
)
from scripts.security import new_trace_id

POLL_INTERVAL  = int(os.environ.get("WORKER_POLL_INTERVAL", "10"))
STALE_INTERVAL = int(os.environ.get("WORKER_STALE_REQUEUE_INTERVAL", "300"))

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".json": "application/json",
    ".srt": "text/plain",
    ".vtt": "text/vtt",
}

_FILE_TYPES = {
    ".mp4": "output",
    ".mov": "output",
    ".json": "enrichment_json",
    ".srt":  "subtitle",
    ".vtt":  "subtitle",
}


def _worker_id() -> str:
    return os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"


def ensure_models_for_mode(mode: str) -> bool:
    """Download required models from S3 before running mode."""
    mode_yaml = _ROOT / "modes" / mode / "mode.yaml"
    if not mode_yaml.exists():
        logger.warning("[worker] no mode.yaml for mode=%s — skipping model download", mode)
        return True
    return download_models_for_mode(mode_yaml)


def process_job(job: dict, worker_id: str) -> None:
    job_id   = job["id"]
    mode     = job["mode"]
    user_id  = job["user_id"]
    params   = job.get("params") or {}
    s3_input = job["s3_input_key"]
    trace_id = new_trace_id()

    logger.info("[worker] starting job_id=%s mode=%s worker=%s trace=%s", job_id, mode, worker_id, trace_id)
    t_start = time.monotonic()

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"sonya_{job_id}_"))
    try:
        # ── 1. Download input ─────────────────────────────────────────────────
        update_job_status(job_id, JOB_STATUS_DOWNLOADING)
        input_ext  = Path(s3_input).suffix or ".mp4"
        input_path = tmp_dir / f"input{input_ext}"
        try:
            download_file(s3_input, input_path)
            logger.info("[worker] input downloaded: %s (%d bytes)", input_path, input_path.stat().st_size)
        except Exception as exc:
            _fail(job_id, "INPUT_DOWNLOAD_FAILED", str(exc), retry=True)
            return

        # ── 2. Ensure models ──────────────────────────────────────────────────
        update_job_status(job_id, JOB_STATUS_MODEL_DOWNLOADING)
        try:
            ensure_models_for_mode(mode)
        except RuntimeError as exc:
            _fail(job_id, "MODEL_DOWNLOAD_FAILED", str(exc), retry=False)
            return
        except Exception as exc:
            logger.warning("[worker] model_download warning job_id=%s: %s — continuing", job_id, exc)

        # ── 3. Run mode ───────────────────────────────────────────────────────
        update_job_status(job_id, JOB_STATUS_MODE_RUNNING)
        output_dir = tmp_dir / "output"
        output_dir.mkdir()

        # Map pipeline step names → status constants
        _STEP_STATUS = {
            "analyzing":  "analyzing",
            "yolo":       "yolo",
            "scripting":  "scripting",
            "tts":        "tts",
            "subtitles":  "subtitles",
            "assembling": "assembling",
        }

        def _progress(step: str, pct: float):
            logger.info("[worker] job_id=%s step=%s progress=%.0f%%", job_id, step, pct * 100)
            mapped = _STEP_STATUS.get(step.lower(), JOB_STATUS_MODE_RUNNING)
            try:
                update_job_status(job_id, mapped)
            except Exception:
                pass

        try:
            runner = get_runner(mode)
            result = runner(
                input_video_path=str(input_path),
                output_dir=str(output_dir),
                params=params,
                progress_callback=_progress,
            )
        except Exception as exc:
            logger.exception("[worker] runner_failed job_id=%s mode=%s: %s", job_id, mode, exc)
            _fail(job_id, "RUNNER_FAILED", str(exc)[:500], retry=False)
            return

        # ── 4. Collect output files ───────────────────────────────────────────
        output_files = list(output_dir.rglob("*"))
        output_files = [f for f in output_files if f.is_file()]

        if not output_files:
            _fail(job_id, "NO_OUTPUT_FILES", "Runner produced no output files", retry=False)
            return

        # ── 5. Upload outputs to S3 ───────────────────────────────────────────
        update_job_status(job_id, JOB_STATUS_UPLOADING_RESULT)
        s3_output_key = None
        enrichment_keys = result.get("enrichment_keys", []) if isinstance(result, dict) else []
        clip_count = 0

        for f in output_files:
            ext = f.suffix.lower()
            ct  = _CONTENT_TYPES.get(ext, "application/octet-stream")
            ft  = _FILE_TYPES.get(ext, "output")
            s3k = build_output_key(user_id=user_id, job_id=job_id, mode=mode, filename=f.name)

            try:
                upload_file(f, s3k, content_type=ct)
                logger.info("[worker] uploaded %s → %s", f.name, s3k)
            except Exception as exc:
                logger.error("[worker] upload_failed file=%s: %s", f.name, exc)
                continue

            # Register file with API
            try:
                add_job_file(
                    job_id=job_id, user_id=user_id,
                    file_type=ft, s3_key=s3k,
                    filename=f.name, content_type=ct,
                    size_bytes=f.stat().st_size,
                )
            except Exception as exc:
                logger.warning("[worker] add_job_file failed file=%s: %s", f.name, exc)

            if ft == "output":
                clip_count += 1
                if s3_output_key is None:
                    s3_output_key = s3k  # first output is the "main" result

        if s3_output_key is None:
            _fail(job_id, "ALL_UPLOADS_FAILED", "No output files could be uploaded", retry=True)
            return

        # ── 6. Complete job ───────────────────────────────────────────────────
        processing_ms = int((time.monotonic() - t_start) * 1000)
        complete_job(
            job_id=job_id,
            s3_output_key=s3_output_key,
            clip_count=clip_count,
            processing_ms=processing_ms,
            enrichment_keys=enrichment_keys,
        )
        logger.info(
            "[worker] job_done job_id=%s clips=%d ms=%d trace=%s",
            job_id, clip_count, processing_ms, trace_id,
        )

    finally:
        # ── 7. Cleanup ────────────────────────────────────────────────────────
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug("[worker] cleaned up tmp_dir=%s", tmp_dir)


def _fail(job_id: str, error_code: str, error_message: str, retry: bool) -> None:
    logger.error("[worker] job_failed job_id=%s code=%s retry=%s msg=%s",
                 job_id, error_code, retry, error_message[:200])
    try:
        fail_job(job_id=job_id, error_code=error_code,
                 error_message=error_message, retry=retry)
    except Exception as exc:
        logger.error("[worker] fail_job_db_error job_id=%s: %s", job_id, exc)


def run_once(job_id: str, worker_id: str) -> None:
    job = claim_specific_job(job_id, worker_id=worker_id)
    if not job:
        job = get_job(job_id)
        if not job:
            raise SystemExit(f"Job {job_id} not found")
        if job.get("status") == JOB_STATUS_CLAIMED and job.get("worker_id") == worker_id:
            logger.info("[worker] re-processing already claimed job_id=%s", job_id)
        else:
            raise SystemExit(
                f"Job {job_id} is in status={job.get('status')} — cannot claim"
            )
    process_job(job, worker_id)


def run_poll(worker_id: str, modes: Optional[List[str]] = None) -> None:
    logger.info("[worker] poll mode started worker_id=%s interval=%ds", worker_id, POLL_INTERVAL)
    last_stale_check = 0.0

    while True:
        # Periodically requeue stale jobs
        now = time.monotonic()
        if now - last_stale_check > STALE_INTERVAL:
            try:
                requeued = requeue_stale_jobs()
                if requeued:
                    logger.info("[worker] requeued %d stale jobs", requeued)
            except Exception as exc:
                logger.warning("[worker] requeue_stale_jobs failed: %s", exc)
            last_stale_check = now

        job = claim_next_pending_job(worker_id=worker_id, modes=modes)
        if job:
            process_job(job, worker_id)
        else:
            time.sleep(POLL_INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="SONYA GPU Worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Process a single job and exit")
    group.add_argument("--poll", action="store_true", help="Poll for jobs continuously")
    parser.add_argument("--job-id",   help="Job ID (required with --once)")
    parser.add_argument("--worker-id", help="Worker identifier (default: hostname-pid)")
    parser.add_argument("--modes",    nargs="+", help="Whitelist of modes to process (--poll only)")
    args = parser.parse_args()

    wid = args.worker_id or _worker_id()
    logger.info("[worker] starting worker_id=%s python=%s host=%s",
                wid, sys.version.split()[0], platform.node())

    if args.once:
        if not args.job_id:
            parser.error("--once requires --job-id")
        run_once(args.job_id, worker_id=wid)
    else:
        run_poll(worker_id=wid, modes=args.modes)


if __name__ == "__main__":
    main()
