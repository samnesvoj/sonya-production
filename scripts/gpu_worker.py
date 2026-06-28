"""
gpu_worker.py
=============
Full-featured GPU worker for SONYA generation pipeline.

Usage:
    python scripts/gpu_worker.py --once --job-id JOB_ID
    python scripts/gpu_worker.py --poll
    python scripts/gpu_worker.py --poll --worker-id my-gpu-node-1

Backend modes (WORKER_BACKEND_MODE env var):
    db   — default; direct PostgreSQL via prod_job_store (requires DATABASE_URL)
    api  — HTTP-only; all job operations go through BACKEND_API_URL worker endpoints
            (requires BACKEND_API_URL + WORKER_SECRET; DATABASE_URL NOT required)
            Used when the GPU instance cannot reach the private PostgreSQL server
            (e.g. vast.ai external GPU provider).

Flow (both modes):
    1. claim_job (specific or next pending)
    2. update status → claimed → downloading
    3. download input from S3
    4. ensure_models_for_mode (model_downloader)
    5. run mode via mode_registry
    6. upload all output files to S3
    7. register each file via backend
    8. complete_job (or fail_job on error)
    9. cleanup temp dir
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# ── Backend mode ───────────────────────────────────────────────────────────────

_WORKER_MODE = os.environ.get("WORKER_BACKEND_MODE", "db").lower()

# Job status string constants — identical to prod_job_store values.
# Defined here so API mode works without importing prod_job_store.
JOB_STATUS_QUEUED            = "queued"
JOB_STATUS_CLAIMED           = "claimed"
JOB_STATUS_COMPLETED         = "completed"
JOB_STATUS_FAILED            = "failed"
JOB_STATUS_DOWNLOADING       = "downloading"
JOB_STATUS_MODEL_DOWNLOADING = "model_downloading"
JOB_STATUS_MODE_RUNNING      = "mode_running"
JOB_STATUS_UPLOADING_RESULT  = "uploading_result"

if _WORKER_MODE == "db":
    from scripts.prod_job_store import (
        add_job_file           as _db_add_file,
        claim_next_pending_job as _db_claim_next,
        claim_specific_job     as _db_claim_specific,
        complete_job           as _db_complete,
        fail_job               as _db_fail,
        get_job                as _db_get_job,
        requeue_stale_jobs     as _db_requeue_stale,
        update_job_status      as _db_update_status,
    )

from scripts.mode_registry import get_runner
from scripts.model_downloader import download_models_for_mode
from scripts.prod_s3_storage import (
    build_output_key,
    download_file,
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
    ".srt": "subtitle",
    ".vtt": "subtitle",
}


def _worker_id() -> str:
    return os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"


# ── API-mode backend client ────────────────────────────────────────────────────

class _BackendAPIClient:
    """
    HTTP client for SONYA worker endpoints.
    Used when WORKER_BACKEND_MODE=api (no direct DB access).
    All requests are authenticated with Bearer WORKER_SECRET.
    """

    def __init__(self) -> None:
        self._base = os.environ.get("BACKEND_API_URL", "").rstrip("/")
        self._secret = os.environ.get("WORKER_SECRET", "")
        if not self._base:
            raise RuntimeError(
                "WORKER_BACKEND_MODE=api requires BACKEND_API_URL to be set"
            )
        if not self._secret:
            raise RuntimeError(
                "WORKER_BACKEND_MODE=api requires WORKER_SECRET to be set"
            )

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._secret}",
        }

    def _post(self, path: str, body: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
        import requests  # type: ignore
        url = f"{self._base}{path}"
        resp = requests.post(url, json=body, headers=self._headers, timeout=timeout)
        if not resp.ok:
            raise RuntimeError(
                f"API {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def claim_job(
        self,
        job_id: Optional[str],
        worker_id: str,
        modes: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"worker_id": worker_id}
        if job_id:
            body["job_id"] = job_id
        if modes:
            body["modes"] = modes
        data = self._post("/api/worker/claim", body)
        return data.get("job")

    def update_status(self, job_id: str, status: str) -> None:
        try:
            self._post(f"/api/worker/jobs/{job_id}/status", {"status": status})
        except Exception as exc:
            logger.warning("[api] update_status failed job_id=%s status=%s: %s", job_id, status, exc)

    def complete_job(
        self,
        job_id: str,
        s3_output_key: str,
        clip_count: int,
        processing_ms: int,
        enrichment_keys: Optional[List[str]] = None,
    ) -> None:
        self._post(
            f"/api/worker/jobs/{job_id}/complete",
            {
                "s3_output_key": s3_output_key,
                "clip_count": clip_count,
                "processing_ms": processing_ms,
                "enrichment_keys": enrichment_keys or [],
            },
        )

    def fail_job(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        retry: bool,
    ) -> None:
        try:
            self._post(
                f"/api/worker/jobs/{job_id}/fail",
                {
                    "error_code": error_code,
                    "error_message": error_message,
                    "retry": retry,
                },
            )
        except Exception as exc:
            logger.error("[api] fail_job endpoint failed job_id=%s: %s", job_id, exc)

    def add_file(
        self,
        job_id: str,
        user_id: str,
        file_type: str,
        s3_key: str,
        filename: str,
        content_type: str,
        size_bytes: int,
    ) -> None:
        try:
            self._post(
                f"/api/worker/jobs/{job_id}/files",
                {
                    "file_type": file_type,
                    "s3_key": s3_key,
                    "filename": filename,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                },
            )
        except Exception as exc:
            logger.warning("[api] add_file failed job_id=%s file=%s: %s", job_id, filename, exc)


# Instantiate the API client lazily (only when mode=api)
_api_client: Optional[_BackendAPIClient] = None


def _get_api_client() -> _BackendAPIClient:
    global _api_client
    if _api_client is None:
        _api_client = _BackendAPIClient()
    return _api_client


# ── Backend-dispatching helpers ────────────────────────────────────────────────

def _update_status(job_id: str, status: str) -> None:
    if _WORKER_MODE == "api":
        _get_api_client().update_status(job_id, status)
    else:
        _db_update_status(job_id, status)


def _do_complete_job(
    job_id: str,
    s3_output_key: str,
    clip_count: int,
    processing_ms: int,
    enrichment_keys: Optional[List[str]],
) -> None:
    if _WORKER_MODE == "api":
        _get_api_client().complete_job(
            job_id, s3_output_key, clip_count, processing_ms, enrichment_keys
        )
    else:
        _db_complete(
            job_id=job_id,
            s3_output_key=s3_output_key,
            clip_count=clip_count,
            processing_ms=processing_ms,
            enrichment_keys=enrichment_keys,
        )


def _do_fail_job(job_id: str, error_code: str, error_message: str, retry: bool) -> None:
    if _WORKER_MODE == "api":
        _get_api_client().fail_job(job_id, error_code, error_message, retry)
    else:
        _db_fail(job_id=job_id, error_code=error_code, error_message=error_message, retry=retry)


def _do_add_file(
    job_id: str,
    user_id: str,
    file_type: str,
    s3_key: str,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> None:
    if _WORKER_MODE == "api":
        _get_api_client().add_file(
            job_id, user_id, file_type, s3_key, filename, content_type, size_bytes
        )
    else:
        _db_add_file(
            job_id=job_id, user_id=user_id,
            file_type=file_type, s3_key=s3_key,
            filename=filename, content_type=content_type,
            size_bytes=size_bytes,
        )


# ── Model download ─────────────────────────────────────────────────────────────

def ensure_models_for_mode(mode: str) -> bool:
    """Download required models from S3 before running mode."""
    mode_yaml = _ROOT / "modes" / mode / "mode.yaml"
    if not mode_yaml.exists():
        logger.warning("[worker] no mode.yaml for mode=%s — skipping model download", mode)
        return True
    return download_models_for_mode(mode_yaml)


# ── Core job processing ────────────────────────────────────────────────────────

def _fail(job_id: str, error_code: str, error_message: str, retry: bool) -> None:
    logger.error(
        "[worker] job_failed job_id=%s code=%s retry=%s msg=%s",
        job_id, error_code, retry, error_message[:200],
    )
    try:
        _do_fail_job(job_id, error_code, error_message, retry)
    except Exception as exc:
        logger.error("[worker] fail_job_backend_error job_id=%s: %s", job_id, exc)


def process_job(job: dict, worker_id: str) -> None:
    job_id  = job["id"]
    mode    = job["mode"]
    user_id = job["user_id"]
    params  = job.get("params") or {}
    s3_input = job["s3_input_key"]
    trace_id = new_trace_id()

    logger.info(
        "[worker] starting job_id=%s mode=%s worker=%s backend=%s trace=%s",
        job_id, mode, worker_id, _WORKER_MODE, trace_id,
    )
    t_start = time.monotonic()
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"sonya_{job_id}_"))

    try:
        # ── 1. Download input ─────────────────────────────────────────────────
        _update_status(job_id, JOB_STATUS_DOWNLOADING)
        input_ext  = Path(s3_input).suffix or ".mp4"
        input_path = tmp_dir / f"input{input_ext}"
        try:
            download_file(s3_input, input_path)
            logger.info(
                "[worker] input downloaded: %s (%d bytes)",
                input_path, input_path.stat().st_size,
            )
        except Exception as exc:
            _fail(job_id, "INPUT_DOWNLOAD_FAILED", str(exc), retry=True)
            return

        # ── 2. Ensure models ──────────────────────────────────────────────────
        _update_status(job_id, JOB_STATUS_MODEL_DOWNLOADING)
        try:
            ensure_models_for_mode(mode)
        except RuntimeError as exc:
            _fail(job_id, "MODEL_DOWNLOAD_FAILED", str(exc), retry=False)
            return
        except Exception as exc:
            logger.warning(
                "[worker] model_download warning job_id=%s: %s — continuing", job_id, exc
            )

        # ── 3. Run mode ───────────────────────────────────────────────────────
        _update_status(job_id, JOB_STATUS_MODE_RUNNING)
        output_dir = tmp_dir / "output"
        output_dir.mkdir()

        _STEP_STATUS = {
            "analyzing":  "analyzing",
            "yolo":       "yolo",
            "scripting":  "scripting",
            "tts":        "tts",
            "subtitles":  "subtitles",
            "assembling": "assembling",
        }

        def _progress(step: str, pct: float) -> None:
            logger.info(
                "[worker] job_id=%s step=%s progress=%.0f%%", job_id, step, pct * 100
            )
            mapped = _STEP_STATUS.get(step.lower(), JOB_STATUS_MODE_RUNNING)
            try:
                _update_status(job_id, mapped)
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
            logger.exception(
                "[worker] runner_failed job_id=%s mode=%s: %s", job_id, mode, exc
            )
            _fail(job_id, "RUNNER_FAILED", str(exc)[:500], retry=False)
            return

        # ── 4. Collect output files ───────────────────────────────────────────
        output_files = [f for f in output_dir.rglob("*") if f.is_file()]
        if not output_files:
            _fail(job_id, "NO_OUTPUT_FILES", "Runner produced no output files", retry=False)
            return

        # ── 5. Upload outputs to S3 ───────────────────────────────────────────
        _update_status(job_id, JOB_STATUS_UPLOADING_RESULT)
        s3_output_key = None
        enrichment_keys = result.get("enrichment_keys", []) if isinstance(result, dict) else []
        clip_count = 0

        for f in output_files:
            ext = f.suffix.lower()
            ct  = _CONTENT_TYPES.get(ext, "application/octet-stream")
            ft  = _FILE_TYPES.get(ext, "output")
            s3k = build_output_key(
                user_id=user_id, job_id=job_id, mode=mode, filename=f.name
            )
            try:
                upload_file(f, s3k, content_type=ct)
                logger.info("[worker] uploaded %s → %s", f.name, s3k)
            except Exception as exc:
                logger.error("[worker] upload_failed file=%s: %s", f.name, exc)
                continue

            _do_add_file(
                job_id=job_id, user_id=user_id,
                file_type=ft, s3_key=s3k,
                filename=f.name, content_type=ct,
                size_bytes=f.stat().st_size,
            )

            if ft == "output":
                clip_count += 1
                if s3_output_key is None:
                    s3_output_key = s3k

        if s3_output_key is None:
            _fail(job_id, "ALL_UPLOADS_FAILED", "No output files could be uploaded", retry=True)
            return

        # ── 6. Complete job ───────────────────────────────────────────────────
        processing_ms = int((time.monotonic() - t_start) * 1000)
        _do_complete_job(
            job_id=job_id,
            s3_output_key=s3_output_key,
            clip_count=clip_count,
            processing_ms=processing_ms,
            enrichment_keys=enrichment_keys,
        )
        logger.info(
            "[worker] job_done job_id=%s clips=%d ms=%d backend=%s trace=%s",
            job_id, clip_count, processing_ms, _WORKER_MODE, trace_id,
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug("[worker] cleaned up tmp_dir=%s", tmp_dir)


# ── Run modes ──────────────────────────────────────────────────────────────────

def run_once(job_id: str, worker_id: str) -> None:
    """Claim a specific job by ID and process it, then exit."""
    if _WORKER_MODE == "api":
        job = _get_api_client().claim_job(job_id=job_id, worker_id=worker_id)
        if not job:
            raise SystemExit(
                f"[worker] API claim returned no job for job_id={job_id} — "
                "job may already be claimed, completed, or not exist"
            )
    else:
        job = _db_claim_specific(job_id, worker_id=worker_id)
        if not job:
            job = _db_get_job(job_id)
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
    """Poll for jobs continuously until SIGTERM / KeyboardInterrupt."""
    logger.info(
        "[worker] poll mode started worker_id=%s backend=%s interval=%ds",
        worker_id, _WORKER_MODE, POLL_INTERVAL,
    )
    last_stale_check = 0.0

    while True:
        # Stale-job requeue — only in DB mode (API server handles this itself)
        if _WORKER_MODE == "db":
            now = time.monotonic()
            if now - last_stale_check > STALE_INTERVAL:
                try:
                    requeued = _db_requeue_stale()
                    if requeued:
                        logger.info("[worker] requeued %d stale jobs", requeued)
                except Exception as exc:
                    logger.warning("[worker] requeue_stale_jobs failed: %s", exc)
                last_stale_check = now

        # Claim next job
        try:
            if _WORKER_MODE == "api":
                job = _get_api_client().claim_job(job_id=None, worker_id=worker_id, modes=modes)
            else:
                job = _db_claim_next(worker_id=worker_id, modes=modes)

            if job:
                process_job(job, worker_id)
            else:
                time.sleep(POLL_INTERVAL)

        except Exception as exc:
            logger.error("[worker] poll_error: %s", exc)
            time.sleep(POLL_INTERVAL)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SONYA GPU Worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Process a single job and exit")
    group.add_argument("--poll", action="store_true", help="Poll for jobs continuously")
    parser.add_argument("--job-id",    help="Job ID (required with --once)")
    parser.add_argument("--worker-id", help="Worker identifier (default: hostname-pid)")
    parser.add_argument(
        "--modes", nargs="+",
        help="Whitelist of modes to process (--poll only)",
    )
    args = parser.parse_args()

    wid = args.worker_id or _worker_id()
    logger.info(
        "[worker] starting worker_id=%s backend_mode=%s python=%s host=%s",
        wid, _WORKER_MODE, sys.version.split()[0], platform.node(),
    )

    if args.once:
        if not args.job_id:
            parser.error("--once requires --job-id")
        run_once(args.job_id, worker_id=wid)
    else:
        run_poll(worker_id=wid, modes=args.modes)


if __name__ == "__main__":
    main()
