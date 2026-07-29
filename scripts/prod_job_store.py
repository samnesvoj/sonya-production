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
  create_job_idempotent  atomic create-or-detect-conflict insert
                         (POST /api/generation/jobs Idempotency-Key,
                         migration 009)
  get_job_by_idempotency_key  fetch the job owning a (user_id, key) pair
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

GPU dispatcher / Vast startup SLA (migration 006 + 007):
  get_stale_gpu_requested_jobs  find jobs stuck past VAST_STARTUP_TIMEOUT_SEC
  mark_gpu_startup_timeout      requeue (another offer) or fail a timed-out job

GPU instance ownership / cleanup (future automatic lifecycle — see
gpu_orchestrator.cleanup_instance_for_terminal_job and
gpu_dispatcher.reconcile_gpu_instance_cleanup; disabled today because the
current production GPU is manually managed, not dispatcher-provisioned):
  get_ephemeral_contract_id           contract_id to destroy for a terminal
                                       job, or None if not a dispatcher-owned
                                       instance
  mark_gpu_instance_cleanup_destroyed record a confirmed destroy (incl. 404)
  mark_gpu_instance_cleanup_error     record a failed destroy attempt
  get_terminal_jobs_pending_gpu_cleanup
                                       dispatcher reconciliation candidates —
                                       terminal + vast_ephemeral + not yet
                                       confirmed destroyed
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
JOB_STATUS_PREFLIGHT_RUNNING = "preflight_running"
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
    JOB_STATUS_WORKER_STARTED, JOB_STATUS_PREFLIGHT_RUNNING, JOB_STATUS_DOWNLOADING,
    JOB_STATUS_MODEL_DOWNLOADING, JOB_STATUS_MODE_RUNNING, JOB_STATUS_ANALYZING,
    JOB_STATUS_YOLO, JOB_STATUS_SCRIPTING, JOB_STATUS_TTS, JOB_STATUS_SUBTITLES,
    JOB_STATUS_ASSEMBLING, JOB_STATUS_UPLOADING_RESULT,
)

_TERMINAL_STATUSES = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)

# GPU instance ownership — stamped into orchestrator_payload.gpu_managed_by
# by gpu_orchestrator._trigger_vast() ONLY when the automatic dispatcher
# provisions a per-job ephemeral vast.ai instance. The current production
# GPU is a manually-provisioned, persistently-running worker
# (WORKER_LOOP=true) that claims jobs itself via claim_next_pending_job() —
# it never goes through mark_gpu_requested(), so its jobs' orchestrator_payload
# is always empty and this stamp never appears for them. See
# get_ephemeral_contract_id() below, which is the single source of truth for
# "is this job's GPU safe to destroy automatically".
GPU_MANAGED_BY_VAST_EPHEMERAL = "vast_ephemeral"

# Cleanup confirmation, stored in the SAME orchestrator_payload JSONB column
# (no schema migration needed — JSONB has no fixed key set). Written by
# gpu_orchestrator.cleanup_instance_for_terminal_job() after every destroy
# attempt, read by get_terminal_jobs_pending_gpu_cleanup() so the dispatcher's
# reconciliation pass stops re-attempting a job once destroy is confirmed.
# Absence of gpu_cleanup_status (or any value other than "destroyed") is
# always treated as "not yet confirmed — retry-eligible", which covers both
# "never attempted" and "attempted and failed" with the same retry behavior.
GPU_CLEANUP_STATUS_DESTROYED = "destroyed"
GPU_CLEANUP_STATUS_ERROR = "error"

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


def create_job_idempotent(
    job_id: str,
    user_id: str,
    mode: str,
    params: Dict[str, Any],
    s3_input_key: str,
    idempotency_key: Optional[str],
    idempotency_fingerprint: Optional[str],
    queue_priority: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    Atomic create-or-detect-conflict insert for POST /api/generation/jobs.

    Returns the newly created row when the INSERT wins (idempotency_key is
    None -- no header supplied, legacy behavior, always wins since NULL
    never conflicts with anything under standard SQL UNIQUE semantics --
    or idempotency_key is set and no row with this (user_id,
    idempotency_key) existed yet).

    Returns None when a row with the same (user_id, idempotency_key)
    already exists -- ON CONFLICT DO NOTHING matched zero rows. The caller
    must then fetch the existing row via get_job_by_idempotency_key() and
    compare idempotency_fingerprint to decide replay (200/202, same job)
    vs conflict (409, different payload under the same key).

    Deliberately does NOT do a SELECT before this INSERT -- conflict
    detection is entirely the database's, via the unique index
    (ux_jobs_user_idempotency_key, migration 009). A SELECT first would
    not protect against two concurrent requests racing past it.

    Deliberately does NOT use ON CONFLICT ... DO UPDATE -- generation_jobs
    has a BEFORE UPDATE trigger (trg_jobs_updated_at) that would bump
    updated_at on the existing row for every duplicate/replayed request,
    even though nothing about that row actually changed.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO generation_jobs
                        (id, user_id, mode, params, s3_input_key, status,
                         queue_priority, created_at, updated_at,
                         idempotency_key, idempotency_fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, idempotency_key) DO NOTHING
                    RETURNING *
                    """,
                    (job_id, user_id, mode, json.dumps(params), s3_input_key,
                     JOB_STATUS_QUEUED, queue_priority, _now(), _now(),
                     idempotency_key, idempotency_fingerprint),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def get_job_by_idempotency_key(user_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the job that owns a given (user_id, idempotency_key) pair --
    called only after create_job_idempotent() reports a conflict, to
    compare idempotency_fingerprint and decide replay vs 409.
    """
    conn = _get_conn()
    try:
        return _row(
            conn,
            "SELECT * FROM generation_jobs WHERE user_id = %s AND idempotency_key = %s",
            (user_id, idempotency_key),
        )
    finally:
        conn.close()


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


# Statuses from which a job may still be claimed by the worker the GPU
# dispatcher already provisioned an instance for. The dispatcher sets
# status=gpu_requested (mark_gpu_requested) immediately after requesting an
# ephemeral instance -- well before that instance boots and its worker gets
# a chance to call /api/worker/claim. If only 'queued' were accepted here,
# the claim would always find 0 matching rows and the job would get stuck
# in gpu_requested until the startup-SLA timeout destroys the instance and
# blacklists the (innocent) host. gpu_booting is included for the same
# reason even though nothing sets it today.
_CLAIMABLE_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_GPU_REQUESTED, JOB_STATUS_GPU_BOOTING)


def claim_specific_job(job_id: str, worker_id: str) -> Optional[Dict[str, Any]]:
    """
    Claim a specific job by ID. Returns None if not claimable.

    Accepts status in _CLAIMABLE_STATUSES (not just 'queued') -- see comment
    above. Also stamps worker_started_at atomically with the claim, so
    get_stale_gpu_requested_jobs() correctly stops treating this job as an
    unclaimed/stuck startup once a worker has actually checked in.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET status            = %s,
                        worker_id         = %s,
                        claimed_at        = %s,
                        started_at        = %s,
                        worker_started_at = %s,
                        updated_at        = %s
                    WHERE id = %s AND status = ANY(%s)
                    RETURNING *
                    """,
                    (JOB_STATUS_CLAIMED, worker_id, _now(), _now(), _now(), _now(),
                     job_id, list(_CLAIMABLE_STATUSES)),
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


def get_stale_gpu_requested_jobs(timeout_sec: int) -> List[Dict[str, Any]]:
    """
    Find jobs stuck in 'gpu_requested' past the production Vast startup SLA.

    A job is a startup_timeout when:
      status = 'gpu_requested'
      gpu_requested_at < NOW() - timeout_sec seconds
      worker_started_at IS NULL

    Used by gpu_dispatcher.cleanup_stale_gpu_requests(), which is run before
    every dispatch pass so a bad instance never blocks the queue for longer
    than timeout_sec.
    """
    conn = _get_conn()
    try:
        return _rows(
            conn,
            """
            SELECT *
            FROM generation_jobs
            WHERE status = 'gpu_requested'
              AND worker_started_at IS NULL
              AND gpu_requested_at IS NOT NULL
              AND gpu_requested_at < NOW() - (%s || ' seconds')::INTERVAL
            """,
            (str(timeout_sec),),
        )
    finally:
        conn.close()


def mark_gpu_startup_timeout(
    job_id: str,
    error: str,
    max_startup_retries: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Resolve a Vast startup-SLA timeout for one job.

    attempts is NOT incremented here — it was already incremented by
    lock_job_for_dispatch() at dispatch time, so it already reflects this
    attempt.

    Retry cap: LEAST(generation_jobs.max_attempts, max_startup_retries) when
    max_startup_retries is given (VAST_MAX_STARTUP_RETRIES), otherwise falls
    back to the job's own max_attempts column.

      attempts <  cap  -> status='queued'  (next dispatcher pass retries a
                          different offer/host), orchestrator_error=
                          'startup timeout; retrying another offer'
      attempts >= cap  -> status='failed', failed_at=NOW()

    Returns the updated row so the caller can log the outcome without a
    second query.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if max_startup_retries is not None:
                    cur.execute(
                        """
                        UPDATE generation_jobs
                        SET
                            error              = %(error)s,
                            orchestrator_error = CASE
                                WHEN attempts >= LEAST(max_attempts, %(cap)s) THEN %(error)s
                                ELSE 'startup timeout; retrying another offer'
                            END,
                            gpu_status = CASE
                                WHEN attempts >= LEAST(max_attempts, %(cap)s) THEN 'startup_timeout_failed'
                                ELSE 'startup_timeout_retry'
                            END,
                            status = CASE
                                WHEN attempts >= LEAST(max_attempts, %(cap)s) THEN 'failed'
                                ELSE 'queued'
                            END,
                            failed_at = CASE
                                WHEN attempts >= LEAST(max_attempts, %(cap)s) THEN NOW()
                                ELSE NULL
                            END,
                            locked_until = NULL,
                            updated_at = NOW()
                        WHERE id = %(job_id)s
                        RETURNING *
                        """,
                        {"error": error[:2000], "cap": max_startup_retries, "job_id": job_id},
                    )
                else:
                    cur.execute(
                        """
                        UPDATE generation_jobs
                        SET
                            error              = %(error)s,
                            orchestrator_error = CASE
                                WHEN attempts >= max_attempts THEN %(error)s
                                ELSE 'startup timeout; retrying another offer'
                            END,
                            gpu_status = CASE
                                WHEN attempts >= max_attempts THEN 'startup_timeout_failed'
                                ELSE 'startup_timeout_retry'
                            END,
                            status = CASE
                                WHEN attempts >= max_attempts THEN 'failed'
                                ELSE 'queued'
                            END,
                            failed_at = CASE
                                WHEN attempts >= max_attempts THEN NOW()
                                ELSE NULL
                            END,
                            locked_until = NULL,
                            updated_at = NOW()
                        WHERE id = %(job_id)s
                        RETURNING *
                        """,
                        {"error": error[:2000], "job_id": job_id},
                    )
                row = cur.fetchone()
                result = dict(row) if row else {}
    finally:
        conn.close()
    logger.warning(
        "[job_store] gpu_startup_timeout job_id=%s new_status=%s attempts=%s/%s",
        job_id, result.get("status"), result.get("attempts"), result.get("max_attempts"),
    )
    return result


# ── GPU instance ownership / cleanup ─────────────────────────────────────────

def _parse_orchestrator_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("orchestrator_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}


def get_ephemeral_contract_id(job: Dict[str, Any]) -> Optional[str]:
    """
    Return the vast.ai contract_id whose ephemeral instance should be
    destroyed now that `job` has reached a terminal state, or None if it
    must NOT be destroyed.

    Fail-safe by construction — returns None (never destroy) unless ALL of:
      - job["status"] is terminal (completed/failed/cancelled). A job still
        mid-flight is never a candidate, no matter what its payload says.
      - job["orchestrator_payload"]["gpu_managed_by"] == GPU_MANAGED_BY_VAST_EPHEMERAL
        — stamped only by gpu_orchestrator._trigger_vast(). Any other value,
        or the key missing entirely (manually-managed GPU, or orchestrator_payload
        itself missing/null/unparseable), returns None.
      - a contract_id is present in that payload.

    Never raises — a malformed payload (bad JSON, wrong type) is treated the
    same as "no payload", i.e. never destroy. This is the single source of
    truth for "is this job's GPU instance safe to destroy automatically";
    callers must not destroy based on any other signal.
    """
    if job.get("status") not in _TERMINAL_STATUSES:
        return None
    payload = _parse_orchestrator_payload(job)
    if payload.get("gpu_managed_by") != GPU_MANAGED_BY_VAST_EPHEMERAL:
        return None
    contract_id = payload.get("contract_id")
    return str(contract_id) if contract_id else None


def _merge_orchestrator_payload(job_id: str, patch: Dict[str, Any]) -> None:
    """
    Shallow-merge `patch` into orchestrator_payload via Postgres JSONB `||`
    (top-level keys in `patch` overwrite existing ones; everything else in
    the column — contract_id, gpu_managed_by, offer/host identifiers — is
    left untouched). Works whether the existing value is NULL or a JSONB
    object. No schema change: orchestrator_payload already has no fixed key
    set (migration 006).
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE generation_jobs
                    SET orchestrator_payload = COALESCE(orchestrator_payload, '{}'::jsonb) || %s::jsonb,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (json.dumps(patch), job_id),
                )
    finally:
        conn.close()


def mark_gpu_instance_cleanup_destroyed(job_id: str) -> None:
    """
    Record that the job's ephemeral vast.ai instance has been confirmed
    destroyed (including "already gone" / 404 — see destroy_vast_instance).
    After this, get_terminal_jobs_pending_gpu_cleanup() no longer returns
    this job, so the dispatcher's reconciliation pass stops re-attempting it.
    """
    _merge_orchestrator_payload(job_id, {
        "gpu_cleanup_status": GPU_CLEANUP_STATUS_DESTROYED,
        "gpu_cleanup_last_attempt_at": _now().isoformat(),
        "gpu_cleanup_error": None,
    })
    logger.info("[job_store] gpu_instance_cleanup_destroyed job_id=%s", job_id)


def mark_gpu_instance_cleanup_error(job_id: str, error: str) -> None:
    """
    Record a failed destroy attempt. Deliberately does NOT touch job
    status/completed_at/failed_at — a Vast API error must never turn a
    completed job into a failed one. gpu_cleanup_status stays anything-but-
    "destroyed", so get_terminal_jobs_pending_gpu_cleanup() keeps returning
    this job for the next dispatcher tick to retry.
    """
    _merge_orchestrator_payload(job_id, {
        "gpu_cleanup_status": GPU_CLEANUP_STATUS_ERROR,
        "gpu_cleanup_last_attempt_at": _now().isoformat(),
        "gpu_cleanup_error": (error or "")[:2000],
    })
    logger.warning("[job_store] gpu_instance_cleanup_error job_id=%s error=%.120s", job_id, error)


def get_terminal_jobs_pending_gpu_cleanup() -> List[Dict[str, Any]]:
    """
    Find terminal jobs whose ephemeral vast.ai instance is not yet confirmed
    destroyed. Used by gpu_dispatcher.reconcile_gpu_instance_cleanup() — the
    durable retry path for cleanup_instance_for_terminal_job(), covering the
    window where the FastAPI BackgroundTask fast path never ran (API process
    crashed/restarted between committing the terminal status and running the
    task) as well as any prior destroy attempt that errored.

    A job is a candidate when ALL of:
      status              terminal (completed/failed/cancelled)
      gpu_managed_by       = 'vast_ephemeral'  (never true for the manually-
                            managed production GPU, or any job with a
                            missing/unrecognized ownership stamp)
      contract_id          present
      gpu_cleanup_status   NOT 'destroyed' (covers both "never attempted"
                            and "attempted and failed" — same retry path)

    Manually-managed and unknown-ownership jobs can never match — there is
    no contract_id/gpu_managed_by stamp for the query to find.
    """
    conn = _get_conn()
    try:
        return _rows(
            conn,
            """
            SELECT *
            FROM generation_jobs
            WHERE status = ANY(%s)
              AND orchestrator_payload->>'gpu_managed_by' = %s
              AND orchestrator_payload->>'contract_id' IS NOT NULL
              AND COALESCE(orchestrator_payload->>'gpu_cleanup_status', '') <> %s
            """,
            (list(_TERMINAL_STATUSES), GPU_MANAGED_BY_VAST_EPHEMERAL, GPU_CLEANUP_STATUS_DESTROYED),
        )
    finally:
        conn.close()


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
    conn = _get_conn()
    try:
        row = _row(
            conn,
            "SELECT COUNT(*) AS n FROM generation_jobs WHERE status = ANY(%s)",
            (list(_ACTIVE_STATUSES),),
        )
        return int(row["n"]) if row else 0
    finally:
        conn.close()
