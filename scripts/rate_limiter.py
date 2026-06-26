"""
rate_limiter.py
===============
Sliding-window rate limiter for SONYA production API.

Backends:
  - PostgreSQL (primary, uses rate_limit_counters table from migration 005)
  - In-memory fallback (single-process only, for dev / if DB unavailable)

Usage (FastAPI dependency):
    from scripts.rate_limiter import RateLimiter
    limiter = RateLimiter(key_prefix="upload", limit=10, window_seconds=60)

    @app.post("/api/generation/jobs")
    async def create_job(request: Request, _: None = Depends(limiter)):
        ...
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from fastapi import HTTPException, Request, status

from scripts.security import new_trace_id

logger = logging.getLogger(__name__)

_RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes")

# ── In-memory fallback —————————————————————————————————————————————————————————

_mem_lock = Lock()
_mem_counts: dict[str, list[float]] = defaultdict(list)


def _mem_check(key: str, limit: int, window_seconds: int) -> bool:
    """Returns True if allowed, False if rate-limited."""
    now = time.monotonic()
    cutoff = now - window_seconds
    with _mem_lock:
        timestamps = _mem_counts[key]
        timestamps[:] = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
        return True


# ── PostgreSQL backend —————————————————————————————————————————————————————————

def _pg_check(key: str, limit: int, window_seconds: int) -> bool:
    """Sliding-window check using rate_limit_counters table. Returns True if allowed."""
    try:
        import psycopg2
        url = os.environ.get("DATABASE_URL")
        if not url:
            return _mem_check(key, limit, window_seconds)
        conn = psycopg2.connect(url)
        window_start = datetime.fromtimestamp(
            int(time.time() / window_seconds) * window_seconds,
            tz=timezone.utc,
        )
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO rate_limit_counters (key, window_start, count)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (key, window_start)
                        DO UPDATE SET count = rate_limit_counters.count + 1
                        RETURNING count
                        """,
                        (key, window_start),
                    )
                    row = cur.fetchone()
                    count = row[0] if row else 1
                    return count <= limit
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[rate_limiter] pg_check failed (%s) — using in-memory fallback", exc)
        return _mem_check(key, limit, window_seconds)


# ── FastAPI dependency class ————————————————————————————————————————————————————

class RateLimiter:
    """
    FastAPI dependency for rate limiting.

    Args:
        key_prefix:      Prefix for rate limit key (e.g. "upload", "api")
        limit:           Max requests per window
        window_seconds:  Window size in seconds
        key_by:          "ip" or "user" — what to rate-limit by
    """

    def __init__(
        self,
        key_prefix: str,
        limit: int = 60,
        window_seconds: int = 60,
        key_by: str = "ip",
    ) -> None:
        self.key_prefix = key_prefix
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_by = key_by

    async def __call__(self, request: Request) -> None:
        if not _RATE_LIMIT_ENABLED:
            return

        if self.key_by == "user":
            identifier = request.headers.get("x-user-id", "anonymous")
        else:
            identifier = request.client.host if request.client else "unknown"

        key = f"{self.key_prefix}:{identifier}"
        allowed = _pg_check(key, self.limit, self.window_seconds)

        if not allowed:
            trace_id = new_trace_id()
            logger.warning(
                "[rate_limiter] rate_limited key=%s limit=%d window=%ds trace_id=%s",
                key, self.limit, self.window_seconds, trace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "rate_limited", "trace_id": trace_id},
                headers={"Retry-After": str(self.window_seconds)},
            )
