"""
security.py
===========
Core security utilities for SONYA production API.

Provides:
  - HMAC-based worker authentication
  - Owner / user_id verification
  - Trace ID generation for error correlation
  - Generic safe error responses (no secret leakage)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

# ── WORKER_SECRET —————————————————————————————————————————————————————————————

_WORKER_SECRET: Optional[str] = None


def _get_worker_secret() -> str:
    global _WORKER_SECRET
    if _WORKER_SECRET is None:
        _WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
    if not _WORKER_SECRET:
        raise RuntimeError("WORKER_SECRET not configured")
    return _WORKER_SECRET


# ── Trace IDs ——————————————————————————————————————————————————————————————————

def new_trace_id() -> str:
    """Generate a short random trace ID for error correlation."""
    return secrets.token_hex(8)


# ── Worker auth ————————————————————————————————————————————————————————————————

def verify_worker_secret(authorization: str = Header(default="")) -> None:
    """
    FastAPI dependency: verify worker HMAC token.
    Header format: Authorization: Bearer <WORKER_SECRET>
    Raises HTTP 403 on failure. Never reveals what was wrong.
    """
    trace_id = new_trace_id()
    expected = f"Bearer {_get_worker_secret()}"
    provided = authorization.strip()

    if not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("[security] worker_auth_failed trace_id=%s", trace_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "trace_id": trace_id},
        )


def verify_worker_hmac(payload: bytes, signature: str) -> bool:
    """
    Verify HMAC-SHA256 signature of a payload using WORKER_SECRET.
    Used for webhook-style worker callbacks.
    """
    secret = _get_worker_secret().encode()
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── User / owner checks ————————————————————————————————————————————————————————

def assert_job_owner(job: dict, user_id: str) -> None:
    """
    Raises HTTP 404 (not 403) if user_id does not own the job.
    Returns 404 to avoid leaking job existence to unauthorized callers.
    """
    if job.get("user_id") != user_id:
        trace_id = new_trace_id()
        logger.warning(
            "[security] owner_mismatch job_id=%s user_id=%s trace_id=%s",
            job.get("id"), user_id, trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "trace_id": trace_id},
        )


def get_user_id(request: Request) -> str:
    """
    Extract user_id from request.
    Reads X-User-Id header (set by upstream auth gateway / API proxy).
    Raises HTTP 401 if missing.
    """
    user_id = request.headers.get("x-user-id", "").strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "trace_id": new_trace_id()},
        )
    return user_id


# ── Safe error responses ———————————————————————————————————————————————————————

def safe_error(message: str, status_code: int = 500, trace_id: Optional[str] = None) -> HTTPException:
    """
    Return a generic HTTPException with trace_id.
    Never includes internal error details, stack traces, or secrets.
    """
    tid = trace_id or new_trace_id()
    logger.error("[security] safe_error status=%d msg=%s trace_id=%s", status_code, message, tid)
    return HTTPException(
        status_code=status_code,
        detail={"error": "internal_error", "trace_id": tid},
    )


# ── CORS / host checks —————————————————————————————————————————————————————————

def get_allowed_origins() -> list[str]:
    """
    Returns the list of allowed CORS origins.

    In APP_ENV=production:
      - CORS_ORIGINS must be set explicitly (e.g. https://sonya-e.com)
      - Wildcard '*' is NOT allowed — raises RuntimeError at startup
    In dev/staging:
      - Falls back to ['*'] with a warning
    """
    raw     = os.environ.get("CORS_ORIGINS", "").strip()
    app_env = os.environ.get("APP_ENV", "development").lower()
    origins = [o.strip() for o in raw.split(",") if o.strip()]

    if not origins:
        if app_env == "production":
            raise RuntimeError(
                "CORS_ORIGINS must be set in production (APP_ENV=production). "
                "Wildcard '*' is not allowed. "
                "Example: CORS_ORIGINS=https://sonya-e.com,https://www.sonya-e.com"
            )
        logger.warning("[security] CORS_ORIGINS not set — using '*' (development only)")
        return ["*"]

    if "*" in origins:
        if app_env == "production":
            raise RuntimeError(
                "CORS_ORIGINS='*' is not allowed in production (APP_ENV=production). "
                "Set explicit origins: CORS_ORIGINS=https://sonya-e.com,https://www.sonya-e.com"
            )
        logger.warning("[security] CORS_ORIGINS='*' — development only, NOT safe for production")

    return origins
