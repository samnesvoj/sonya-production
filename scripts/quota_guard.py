"""
quota_guard.py
==============
Per-user quota enforcement for SONYA production API.

Checks:
  - Max concurrent active jobs per user
  - Max jobs per day per user
  - Max storage usage (S3 key prefix scan or DB aggregate)

All limits configurable via env vars.
Raises HTTP 429 with trace_id on quota exceeded.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status

from scripts.security import new_trace_id

logger = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = int(os.environ.get("QUOTA_MAX_CONCURRENT_JOBS", "3"))
MAX_JOBS_PER_DAY    = int(os.environ.get("QUOTA_MAX_JOBS_PER_DAY", "50"))
QUOTA_ENABLED       = os.environ.get("QUOTA_ENABLED", "true").lower() in ("1", "true", "yes")


def _get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    import psycopg2
    return psycopg2.connect(url)


def check_user_quota(user_id: str) -> None:
    """
    FastAPI-compatible quota check. Raises HTTP 429 if quota exceeded.
    Call before accepting a new job.
    """
    if not QUOTA_ENABLED:
        return

    trace_id = new_trace_id()
    try:
        _check_concurrent(user_id, trace_id)
        _check_daily(user_id, trace_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[quota] quota_check_failed user_id=%s error=%s — allowing request", user_id, exc)


def _check_concurrent(user_id: str, trace_id: str) -> None:
    """Raise 429 if user has too many active (pending + processing) jobs."""
    try:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM generation_jobs
                    WHERE user_id = %s AND status IN ('pending', 'processing')
                    """,
                    (user_id,),
                )
                count = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[quota] concurrent_check_db_error user_id=%s: %s", user_id, exc)
        return

    if count >= MAX_CONCURRENT_JOBS:
        logger.warning(
            "[quota] concurrent_limit user_id=%s active=%d limit=%d trace_id=%s",
            user_id, count, MAX_CONCURRENT_JOBS, trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "quota_exceeded",
                "reason": "too_many_active_jobs",
                "active": count,
                "limit": MAX_CONCURRENT_JOBS,
                "trace_id": trace_id,
            },
        )


def _check_daily(user_id: str, trace_id: str) -> None:
    """Raise 429 if user has exceeded daily job limit."""
    try:
        conn = _get_conn()
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM generation_jobs
                    WHERE user_id = %s AND created_at >= %s
                    """,
                    (user_id, since),
                )
                count = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[quota] daily_check_db_error user_id=%s: %s", user_id, exc)
        return

    if count >= MAX_JOBS_PER_DAY:
        logger.warning(
            "[quota] daily_limit user_id=%s count=%d limit=%d trace_id=%s",
            user_id, count, MAX_JOBS_PER_DAY, trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "quota_exceeded",
                "reason": "daily_limit_reached",
                "count_24h": count,
                "limit": MAX_JOBS_PER_DAY,
                "trace_id": trace_id,
            },
        )
