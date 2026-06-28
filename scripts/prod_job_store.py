"""
prod_job_store.py
=================
PostgreSQL job store for SONYA generation pipeline.

Tables (from migrations):
  generation_jobs  — job lifecycle
  generation_files — per-job S3 file registry

Status lifecycle:
  queued → claimed → downloading → model_downloading → mode_running
  → analyzing → yolo → scripting → tts → subtitles → assembling
  → uploading_result → completed
  (any step) → failed

Functions:
  create_job             create a new queued job
  get_job                fetch single job by id
  list_user_jobs         paginated list for a user
  update_job_status      granular status update (any status constant)
  complete_job           mark completed, set output key + metadata
  fail_job               mark failed, optionally requeue as queued
  claim_next_pending_job FOR UPDATE SKIP LOCKED — poll mode
  claim_specific_job     claim a known job_id — --once mode
  requeue_stale_jobs     reset stuck jobs → queued
  add_job_file           register an S3 file with a job
  list_job_files         list all files for a job
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Status constants ——————————————————————————————————————————————————————————

JOB_STATUS_QUEUED            = "queued"
JOB_STATUS_CLAIMED           = "claimed"
JOB_STATUS_GPU_REQUESTED     = "gpu_requested"
JOB_STATUS_GPU_BOOTING       = "gpu_booting"
JOB_STATUS_WORKER_STARTED    = "worker_started"
JOB_STATUS_DOWNLOADING       = "downloading"
JOB_STATUS_MODEL_DOWNLOADING = "model_downloading"
JOB_STATUS_MODE_RUNNING      = "mode_running"
JOB_STATUS_ANALYZING         = "analyzing"
JOB_STATUS_YOLO              = "yolo"
JOB_STATUS_SCRIPTING         = "scripting"
JOB_STATUS_TTS               = "tts"
JOB_STATUS_SUBTITLES         = "subtitles"
JOB_STATUS_ASSEMBLING        = "assembling"
JOB_STATUS_UPLOADING_RESULT  = "uploading_result"
JOB_STATUS_COMPLETED         = "completed"
JOB_STATUS_FAILED            = "failed"
JOB_STATUS_CANCELLED         = "cancelled"

_ACTIVE_STATUSES = (
    JOB_STATUS_CLAIMED, JOB_STATUS_GPU_REQUESTED, JOB_STATUS_GPU_BOOTING,
    JOB_STATUS_WORKER_STARTED, JOB_STATUS_DOWNLOADING, JOB_STATUS_MODEL_DOWNLOADING,
    JOB_STATUS_MODE_RUNNING, JOB_STATUS_ANALYZING, JOB_STATUS_YOLO,
    JOB_STATUS_SCRIPTING, JOB_STATUS_TTS, JOB_STATUS_SUBTITLES,
    JOB_STATUS_ASSEMBLING, JOB_STATUS_UPLOADING_RESULT,
)

# ── DB helpers —————————————————————————————————————————————————————————————————

_DB_AVAILABLE = False
try:
    import psycopg2
    import psycopg2.extras
    _DB_AVAILABLE = True
except ImportError:
    pass


def _get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    if not _DB_AVAILABLE:
        raise RuntimeError("psycopg2 not installed — run: pip install psycopg2-binary")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(conn, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def _rows(conn, sql: str, params: tuple) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ── Job CRUD ———————————————————————————————————————————————————————————————————

def create_job(
    job_id: str,
    user_id: str,
    mode: str,
    params: Dict[str, Any],
    s3_input_key: str,
    queue_priority: int = 0,
) -> str:
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO generation_jobs
                        (id, user_id, mode, params, s3_input_key, status,
                         queue_priority, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (job_id, user_id, mode, json.dumps(params), s3_input_key,
                     JOB_STATUS_QUEUED, queue_priority, _now(), _now()),
                )
    finally:
        conn.close()
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    try:
        return _row(conn, "SELECT * FROM generation_jobs WHERE id = %s", (job_id,))
    finally:
        conn.close()


def list_user_jobs(
    user_id: str,
    limit: int = 20,
    offset: int = 0,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    conn = _get_conn()
    try:
        if status:
            sql = """
                SELECT * FROM generation_jobs
                WHERE user_id = %s AND status = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """
            params = (user_id, status, limit, offset)
        else:
            sql = """
                SELECT * FROM generation_jobs
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """
            params = (user_id, limit, offset)
        return _rows(conn, sql, params)
    finally:
        conn.close()


def update_job_status(job_id: str, status: str) -> None:
    """Update job to any valid status (including granular pipeline steps)."""
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE generation_jobs SET status=%s, updated_at=%s WHERE id=%s",
                    (status, _now(), job_id),
                )
    finally:
        conn.close()


def complete_job(
    job_id: str,
    s3_output_key: str,
    clip_count: Optional[int] = None,
    processing_ms: Optional[int] = None,
    enrichment_keys: Optional[List[str]] = None,
) -> None:
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs SET
                        status          = %s,
                        s3_output_key   = %s,
                        clip_count      = %s,
                        processing_ms   = %s,
                        enrichment_keys = %s,
                        completed_at    = %s,
                        updated_at      = %s
                    WHERE id = %s
                    """,
                    (JOB_STATUS_COMPLETED, s3_output_key, clip_count, processing_ms,
                     enrichment_keys or [], _now(), _now(), job_id),
                )
    finally:
        conn.close()


def fail_job(
    job_id: str,
    error_code: str,
    error_message: str,
    retry: bool = True,
) -> None:
    """
    Mark job as failed.
    If retry=True and retry_count < max_retries: requeue as 'queued'.
    If retry=False or exhausted: mark 'failed' permanently.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT retry_count, max_retries FROM generation_jobs WHERE id=%s",
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    logger.warning("[job_store] fail_job: job not found id=%s", job_id)
                    return
                retry_count = row["retry_count"]
                max_retries = row["max_retries"]
                can_retry   = retry and (retry_count < max_retries)
                new_status  = JOB_STATUS_QUEUED if can_retry else JOB_STATUS_FAILED

                cur.execute(
                    """
                    UPDATE generation_jobs SET
                        status      = %s,
                        last_error  = %s,
                        error       = %s,
                        retry_count = retry_count + 1,
                        updated_at  = %s
                    WHERE id = %s
                    """,
                    (new_status, error_code, error_message[:2000], _now(), job_id),
                )
                if can_retry:
                    logger.info("[job_store] job requeued job_id=%s attempt=%d/%d",
                                job_id, retry_count + 1, max_retries)
                else:
                    logger.warning("[job_store] job permanently failed job_id=%s code=%s",
                                   job_id, error_code)
    finally:
        conn.close()


# ── Claim / poll ———————————————————————————————————————————————————————————————

def claim_next_pending_job(
    worker_id: str,
    modes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Atomically claim the next queued job using FOR UPDATE SKIP LOCKED.
    Transitions status: queued → claimed.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if modes:
                    cur.execute(
                        """
                        UPDATE generation_jobs
                        SET status     = %s,
                            worker_id  = %s,
                            claimed_at = %s,
                            started_at = %s,
                            updated_at = %s
                        WHERE id = (
                            SELECT id FROM generation_jobs
                            WHERE status = %s AND mode = ANY(%s)
                            ORDER BY queue_priority DESC, created_at ASC
                            LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING *
                        """,
                        (JOB_STATUS_CLAIMED, worker_id, _now(), _now(), _now(),
                         JOB_STATUS_QUEUED, list(modes)),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE generation_jobs
                        SET status     = %s,
                            worker_id  = %s,
                            claimed_at = %s,
                            started_at = %s,
                            updated_at = %s
                        WHERE id = (
                            SELECT id FROM generation_jobs
                            WHERE status = %s
                            ORDER BY queue_priority DESC, created_at ASC
                            LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING *
                        """,
                        (JOB_STATUS_CLAIMED, worker_id, _now(), _now(), _now(),
                         JOB_STATUS_QUEUED),
                    )
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def claim_specific_job(job_id: str, worker_id: str) -> Optional[Dict[str, Any]]:
    """Claim a specific queued job. Returns None if not claimable."""
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET status     = %s,
                        worker_id  = %s,
                        claimed_at = %s,
                        started_at = %s,
                        updated_at = %s
                    WHERE id = %s AND status = %s
                    RETURNING *
                    """,
                    (JOB_STATUS_CLAIMED, worker_id, _now(), _now(), _now(),
                     job_id, JOB_STATUS_QUEUED),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def requeue_stale_jobs(stale_minutes: int = 30) -> int:
    """
    Reset stuck active jobs → queued when stuck longer than stale_minutes.
    Returns count of requeued jobs.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                active_list = list(_ACTIVE_STATUSES)
                placeholders = ",".join(["%s"] * len(active_list))
                cur.execute(
                    f"""
                    UPDATE generation_jobs
                    SET status      = %s,
                        worker_id   = NULL,
                        claimed_at  = NULL,
                        updated_at  = %s
                    WHERE status IN ({placeholders})
                      AND claimed_at < NOW() - INTERVAL '%s minutes'
                      AND retry_count < max_retries
                    """,
                    (JOB_STATUS_QUEUED, _now(), *active_list, stale_minutes),
                )
                return cur.rowcount
    finally:
        conn.close()


# ── Files ——————————————————————————————————————————————————————————————————————

def add_job_file(
    job_id: str,
    user_id: str,
    file_type: str,
    s3_key: str,
    filename: str,
    content_type: str = "application/octet-stream",
    size_bytes: Optional[int] = None,
    duration_sec: Optional[float] = None,
) -> str:
    file_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO generation_files
                        (id, job_id, user_id, file_type, s3_key, filename,
                         content_type, size_bytes, duration_sec, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (s3_key) DO NOTHING
                    """,
                    (file_id, job_id, user_id, file_type, s3_key, filename,
                     content_type, size_bytes, duration_sec, _now()),
                )
    finally:
        conn.close()
    return file_id


def list_job_files(job_id: str) -> List[Dict[str, Any]]:
    conn = _get_conn()
    try:
        return _rows(conn,
                     "SELECT * FROM generation_files WHERE job_id=%s ORDER BY created_at ASC",
                     (job_id,))
    finally:
        conn.close()


# ── GPU dispatcher queue API (migration 006) ————————————————————————————————————

def get_next_queued_job_for_dispatch() -> Optional[Dict[str, Any]]:
    """
    Peek at the next dispatchable job without locking it.

    Selects status='queued', attempts < max_attempts,
    locked_until IS NULL or expired,
    ordered by priority DESC then queued_at ASC (FIFO within same priority).

    Returns the row dict or None.  Use lock_job_for_dispatch to atomically
    acquire the job before calling the orchestrator.
    """
    conn = _get_conn()
    try:
        return _row(
            conn,
            """
            SELECT *
            FROM generation_jobs
            WHERE status = 'queued'
              AND attempts < max_attempts
              AND (locked_until IS NULL OR locked_until < NOW())
            ORDER BY priority DESC, queued_at ASC
            LIMIT 1
            """,
            (),
        )
    finally:
        conn.close()


def lock_job_for_dispatch(job_id: str, lock_seconds: int = 120) -> Optional[Dict[str, Any]]:
    """
    Atomically lock a queued job for the dispatcher.

    Uses SELECT … FOR UPDATE SKIP LOCKED so two dispatcher instances never
    race on the same row.  Increments attempts and sets locked_until.

    Returns the updated row, or None if the job was already taken.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET
                        attempts     = attempts + 1,
                        locked_until = NOW() + (%s || ' seconds')::INTERVAL,
                        updated_at   = NOW()
                    WHERE id = (
                        SELECT id
                        FROM generation_jobs
                        WHERE id = %s
                          AND status = 'queued'
                          AND attempts < max_attempts
                          AND (locked_until IS NULL OR locked_until < NOW())
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING *
                    """,
                    (str(lock_seconds), job_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def mark_gpu_requested(
    job_id: str,
    orchestrator_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Set status=gpu_requested, gpu_status=requested, record payload + timestamp."""
    import json as _json
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET
                        status               = 'gpu_requested',
                        gpu_status           = 'requested',
                        gpu_requested_at     = NOW(),
                        locked_until         = NULL,
                        orchestrator_payload = %s,
                        orchestrator_error   = NULL,
                        updated_at           = NOW()
                    WHERE id = %s
                    """,
                    (_json.dumps(orchestrator_payload or {}), job_id),
                )
    finally:
        conn.close()
    logger.info("[job_store] gpu_requested job_id=%s", job_id)


def mark_gpu_request_failed(job_id: str, error: str) -> None:
    """
    Record a failed GPU orchestration attempt.

    If attempts >= max_attempts → status='failed' + failed_at.
    Otherwise → status='queued' for dispatcher retry.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET
                        gpu_status         = 'request_failed',
                        orchestrator_error = %s,
                        locked_until       = NULL,
                        status = CASE
                            WHEN attempts >= max_attempts THEN 'failed'
                            ELSE 'queued'
                        END,
                        failed_at = CASE
                            WHEN attempts >= max_attempts THEN NOW()
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (error[:2000], job_id),
                )
    finally:
        conn.close()
    logger.warning("[job_store] gpu_request_failed job_id=%s error=%.120s", job_id, error)


def mark_worker_started(job_id: str) -> None:
    """Transition to worker_started and record timestamp."""
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET
                        status            = 'worker_started',
                        gpu_status        = 'worker_running',
                        worker_started_at = NOW(),
                        updated_at        = NOW()
                    WHERE id = %s
                    """,
                    (job_id,),
                )
    finally:
        conn.close()
    logger.info("[job_store] worker_started job_id=%s", job_id)


def mark_job_completed(
    job_id: str,
    s3_output_key: str,
    clip_count: Optional[int] = None,
    processing_ms: Optional[int] = None,
) -> None:
    """Dispatcher-friendly complete: delegates to complete_job + sets gpu_status=done."""
    complete_job(
        job_id=job_id,
        s3_output_key=s3_output_key,
        clip_count=clip_count,
        processing_ms=processing_ms,
    )
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE generation_jobs SET gpu_status='gpu_completed', updated_at=NOW() WHERE id=%s",
                    (job_id,),
                )
    finally:
        conn.close()
    logger.info("[job_store] job_completed_dispatcher job_id=%s", job_id)


def mark_job_failed(job_id: str, error_code: str, error_message: str) -> None:
    """Dispatcher-friendly fail: delegates to fail_job + sets gpu_status=failed + failed_at."""
    fail_job(
        job_id=job_id,
        error_code=error_code,
        error_message=error_message,
        retry=False,
    )
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET gpu_status='failed', failed_at=NOW(), updated_at=NOW()
                    WHERE id=%s
                    """,
                    (job_id,),
                )
    finally:
        conn.close()
    logger.warning("[job_store] job_failed_dispatcher job_id=%s", job_id)


def count_active_gpu_jobs() -> int:
    """
    Count jobs currently in GPU-active states.
    Used by the dispatcher to enforce MAX_ACTIVE_GPU_JOBS concurrency cap.
    """
    gpu_active = ("gpu_requested", "gpu_booting", "worker_started", "model_downloading")
    conn = _get_conn()
    try:
        row = _row(
            conn,
            "SELECT COUNT(*) AS n FROM generation_jobs WHERE status = ANY(%s)",
            (list(gpu_active),),
        )
        return int(row["n"]) if row else 0
    finally:
        conn.close()
