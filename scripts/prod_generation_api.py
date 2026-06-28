"""
prod_generation_api.py
======================
Full production FastAPI for SONYA generation pipeline.

Public endpoints (require X-User-Id header from auth gateway):
  GET  /health
  GET  /api/health
  POST /api/generation/jobs
  POST /api/trailer/jobs
  GET  /api/generation/jobs/{job_id}
  GET  /api/generation/jobs/{job_id}/result-url
  GET  /api/generation/jobs          (list user jobs)

Worker-internal endpoints (require Authorization: Bearer WORKER_SECRET):
  POST /api/worker/claim
  POST /api/worker/jobs/{job_id}/status
  POST /api/worker/jobs/{job_id}/complete
  POST /api/worker/jobs/{job_id}/fail
  POST /api/worker/jobs/{job_id}/files

File upload only — no URL-based video fetching via any external tool.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scripts.prod_job_store import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_CLAIMED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    add_job_file,
    claim_next_pending_job,
    claim_specific_job,
    complete_job,
    create_job,
    fail_job,
    get_job,
    list_job_files,
    list_user_jobs,
    update_job_status,
)
from scripts.prod_s3_storage import (
    build_input_key,
    build_output_key,
    generate_presigned_get_url,
    upload_bytes,
)
from scripts.quota_guard import check_user_quota
from scripts.rate_limiter import RateLimiter
from scripts.security import (
    assert_job_owner,
    get_allowed_origins,
    get_user_id,
    new_trace_id,
    safe_error,
    verify_worker_secret,
)

# ── Priority table (server-side, from X-User-Plan header) ──────────────────────
_PLAN_PRIORITY: dict[str, int] = {
    "admin":   1000,
    "pro":      500,
    "paid":     300,
    "free":     100,
    "unknown":  100,
}


def _resolve_priority(request: Request) -> tuple[int, str]:
    """Return (priority, plan) from X-User-Plan header. Always server-assigned."""
    plan = (request.headers.get("X-User-Plan") or "unknown").lower().strip()
    if plan not in _PLAN_PRIORITY:
        plan = "unknown"
    return _PLAN_PRIORITY[plan], plan
from scripts.security_audit import (
    EVT_JOB_CREATED,
    EVT_JOB_CLAIMED,
    EVT_JOB_COMPLETED,
    EVT_JOB_FAILED,
    EVT_UPLOAD_REJECTED,
    audit,
)
from scripts.upload_security import validate_upload

logger = logging.getLogger(__name__)

# ── App ————————————————————————————————————————————————————————————————————————

app = FastAPI(
    title="SONYA Generation API",
    version="1.0.0",
    docs_url="/docs" if os.environ.get("ENABLE_DOCS", "false").lower() == "true" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-User-Id", "X-Request-Id"],
)

# ── Mode registry ——————————————————————————————————————————————————————————————

ALLOWED_MODES = {
    "trailer_film_breaker",
    "virality",
    "stories",
    "educational",
    "streamer",
    "sonya_gen",
}

# ── Rate limiters ——————————————————————————————————————————————————————————————

_upload_limiter = RateLimiter(key_prefix="upload", limit=20, window_seconds=3600, key_by="user")
_api_limiter    = RateLimiter(key_prefix="api",    limit=120, window_seconds=60,   key_by="user")

# ── Pydantic models ————————————————————————————————————————————————————————————

class JobResponse(BaseModel):
    job_id: str
    status: str
    mode: str
    created_at: Optional[str] = None


class JobStatusUpdate(BaseModel):
    status: str
    progress: Optional[float] = None
    message: Optional[str] = None


class JobCompleteRequest(BaseModel):
    s3_output_key: str
    clip_count: Optional[int] = None
    processing_ms: Optional[int] = None
    enrichment_keys: Optional[List[str]] = None


class JobFailRequest(BaseModel):
    error_code: str = "UNKNOWN_ERROR"
    error_message: str = "Job failed"
    retry: bool = True


class JobFileRequest(BaseModel):
    file_type: str
    s3_key: str
    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: Optional[int] = None
    duration_sec: Optional[float] = None


class WorkerClaimRequest(BaseModel):
    worker_id: str
    job_id: Optional[str] = None
    modes: Optional[List[str]] = None


# ── Health ——————————————————————————————————————————————————————————————————————

@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "sonya-generation-api", "version": "1.0.0"}


# ── Public: Job creation ————————————————————————————————————————————————————————

@app.post("/api/generation/jobs", response_model=JobResponse)
async def create_generation_job(
    request: Request,
    mode: str = Form(...),
    file: UploadFile = File(...),
    params: Optional[str] = Form(None),
    _rl: None = Depends(_upload_limiter),
):
    user_id  = get_user_id(request)
    trace_id = new_trace_id()

    # Priority assigned server-side — clients cannot override
    priority, plan = _resolve_priority(request)

    # Mode validation
    if mode not in ALLOWED_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_mode", "allowed": sorted(ALLOWED_MODES), "trace_id": trace_id},
        )

    # Params parsing
    parsed_params: Dict[str, Any] = {}
    if params:
        try:
            parsed_params = json.loads(params)
            if not isinstance(parsed_params, dict):
                raise ValueError("params must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_params", "detail": str(exc), "trace_id": trace_id},
            )

    # Quota check
    check_user_quota(user_id)

    # Upload validation (magic bytes, size, extension)
    try:
        content, safe_name = await validate_upload(file)
    except HTTPException as exc:
        audit(EVT_UPLOAD_REJECTED, user_id=user_id, trace_id=trace_id,
              details={"mode": mode, "reason": str(exc.detail)},
              ip_address=request.client.host if request.client else None)
        raise

    # Upload to S3
    ext     = Path(safe_name).suffix or ".mp4"
    job_id  = str(uuid.uuid4())
    s3_key  = build_input_key(user_id=user_id, job_id=job_id, mode=mode, ext=ext)

    try:
        upload_bytes(content, s3_key, content_type=file.content_type or "video/mp4")
    except Exception as exc:
        logger.error("[api] s3_upload_failed job_id=%s trace_id=%s: %s", job_id, trace_id, exc)
        raise safe_error("storage_error", 500, trace_id)

    # Create job in DB — GPU is never triggered here; the dispatcher service
    # picks up queued jobs and calls the GPU orchestrator asynchronously.
    try:
        job_id = create_job(
            job_id=job_id,
            user_id=user_id,
            mode=mode,
            params=parsed_params,
            s3_input_key=s3_key,
            queue_priority=priority,  # existing column (migration 003)
        )
    except Exception as exc:
        logger.error("[api] create_job_failed trace_id=%s: %s", trace_id, exc)
        raise safe_error("db_error", 500, trace_id)

    # Persist migration-006 columns (priority + plan) — best-effort
    try:
        from scripts.prod_job_store import _get_conn  # type: ignore
        conn = _get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE generation_jobs SET priority=%s, plan=%s WHERE id=%s",
                        (priority, plan, job_id),
                    )
        finally:
            conn.close()
    except Exception as _exc:
        logger.warning("[api] priority_col_update_skipped job_id=%s exc=%s", job_id, _exc)

    # Register input file
    try:
        add_job_file(
            job_id=job_id, user_id=user_id,
            file_type="input", s3_key=s3_key,
            filename=safe_name, content_type=file.content_type or "video/mp4",
            size_bytes=len(content),
        )
    except Exception as exc:
        logger.warning("[api] add_input_file_failed job_id=%s: %s", job_id, exc)

    audit(EVT_JOB_CREATED, user_id=user_id, job_id=job_id, trace_id=trace_id,
          details={"mode": mode, "size_bytes": len(content), "plan": plan, "priority": priority},
          ip_address=request.client.host if request.client else None)

    logger.info(
        "[api] job_created job_id=%s user_id=%s mode=%s plan=%s priority=%d size=%d",
        job_id, user_id, mode, plan, priority, len(content),
    )

    job = get_job(job_id)
    return JobResponse(
        job_id=job_id,
        status=JOB_STATUS_QUEUED,
        mode=mode,
        created_at=str(job.get("created_at")) if job else None,
    )


@app.post("/api/trailer/jobs", response_model=JobResponse)
async def create_trailer_job(
    request: Request,
    file: UploadFile = File(...),
    params: Optional[str] = Form(None),
    _rl: None = Depends(_upload_limiter),
):
    """Alias: mode=trailer_film_breaker."""
    return await create_generation_job(
        request=request, mode="trailer_film_breaker",
        file=file, params=params, _rl=None,
    )


# ── Public: Job status / result ————————————————————————————————————————————————

@app.get("/api/generation/jobs/{job_id}")
async def get_generation_job(
    request: Request,
    job_id: str,
    _rl: None = Depends(_api_limiter),
):
    user_id = get_user_id(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found", "trace_id": new_trace_id()})
    assert_job_owner(job, user_id)

    files = list_job_files(job_id)
    return {**job, "files": files}


@app.get("/api/generation/jobs/{job_id}/result-url")
async def get_result_url(
    request: Request,
    job_id: str,
    expires: int = Query(default=3600, ge=60, le=86400),
):
    user_id = get_user_id(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found", "trace_id": new_trace_id()})
    assert_job_owner(job, user_id)

    if job.get("status") != JOB_STATUS_COMPLETED:
        raise HTTPException(
            status_code=400,
            detail={"error": "job_not_done", "status": job.get("status"), "trace_id": new_trace_id()},
        )

    s3_key = job.get("s3_output_key")
    if not s3_key:
        raise HTTPException(status_code=404, detail={"error": "no_output", "trace_id": new_trace_id()})

    try:
        url = generate_presigned_get_url(s3_key, expires_in=expires)
    except Exception as exc:
        logger.error("[api] presign_failed job_id=%s: %s", job_id, exc)
        raise safe_error("storage_error", 500)

    return {"url": url, "expires_in": expires}


@app.get("/api/generation/jobs")
async def list_jobs(
    request: Request,
    limit: int  = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    _rl: None = Depends(_api_limiter),
):
    user_id = get_user_id(request)
    jobs = list_user_jobs(user_id, limit=limit, offset=offset, status=status_filter)
    return {"jobs": jobs, "count": len(jobs)}


# ── Worker endpoints (require WORKER_SECRET) ————————————————————————————————————

@app.post("/api/worker/claim")
async def worker_claim(
    body: WorkerClaimRequest,
    _auth: None = Depends(verify_worker_secret),
):
    """Worker claims the next available pending job, or a specific job_id."""
    trace_id = new_trace_id()

    if body.job_id:
        job = claim_specific_job(body.job_id, worker_id=body.worker_id)
    else:
        job = claim_next_pending_job(worker_id=body.worker_id, modes=body.modes)

    if not job:
        return {"job": None}

    audit(EVT_JOB_CLAIMED, job_id=job.get("id"), trace_id=trace_id,
          details={"worker_id": body.worker_id, "mode": job.get("mode")})

    return {"job": job}


@app.post("/api/worker/jobs/{job_id}/status")
async def worker_update_status(
    job_id: str,
    body: JobStatusUpdate,
    _auth: None = Depends(verify_worker_secret),
):
    _VALID_STATUSES = {
        "queued", "claimed", "gpu_requested", "gpu_booting", "worker_started",
        "downloading", "model_downloading", "mode_running", "analyzing", "yolo",
        "scripting", "tts", "subtitles", "assembling", "uploading_result",
        "completed", "failed", "cancelled",
    }
    if body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail={"error": "invalid_status"})
    update_job_status(job_id, body.status)
    return {"ok": True, "job_id": job_id, "status": body.status}


@app.post("/api/worker/jobs/{job_id}/complete")
async def worker_complete_job(
    job_id: str,
    body: JobCompleteRequest,
    _auth: None = Depends(verify_worker_secret),
):
    trace_id = new_trace_id()
    complete_job(
        job_id=job_id,
        s3_output_key=body.s3_output_key,
        clip_count=body.clip_count,
        processing_ms=body.processing_ms,
        enrichment_keys=body.enrichment_keys,
    )
    job = get_job(job_id)
    audit(EVT_JOB_COMPLETED, user_id=job.get("user_id") if job else None,
          job_id=job_id, trace_id=trace_id,
          details={"clip_count": body.clip_count, "processing_ms": body.processing_ms})
    logger.info("[api] job_completed job_id=%s clips=%s ms=%s", job_id, body.clip_count, body.processing_ms)
    return {"ok": True, "job_id": job_id}


@app.post("/api/worker/jobs/{job_id}/fail")
async def worker_fail_job(
    job_id: str,
    body: JobFailRequest,
    _auth: None = Depends(verify_worker_secret),
):
    trace_id = new_trace_id()
    fail_job(
        job_id=job_id,
        error_code=body.error_code,
        error_message=body.error_message,
        retry=body.retry,
    )
    job = get_job(job_id)
    audit(EVT_JOB_FAILED, user_id=job.get("user_id") if job else None,
          job_id=job_id, trace_id=trace_id,
          details={"error_code": body.error_code, "retry": body.retry})
    logger.warning("[api] job_failed job_id=%s code=%s retry=%s", job_id, body.error_code, body.retry)
    return {"ok": True, "job_id": job_id}


@app.post("/api/worker/jobs/{job_id}/files")
async def worker_add_file(
    job_id: str,
    body: JobFileRequest,
    _auth: None = Depends(verify_worker_secret),
):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    add_job_file(
        job_id=job_id,
        user_id=job["user_id"],
        file_type=body.file_type,
        s3_key=body.s3_key,
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        duration_sec=body.duration_sec,
    )
    return {"ok": True, "job_id": job_id, "file_type": body.file_type}
