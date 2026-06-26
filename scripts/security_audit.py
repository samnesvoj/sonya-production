"""
security_audit.py
=================
Security audit logging for SONYA production API.

Logs structured security events to:
  - PostgreSQL security_audit_log table (from migration 005)
  - Python logger (always, as fallback)

No secrets, tokens, or full file paths are stored.
Only metadata: event_type, user_id, job_id, ip, trace_id.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_AUDIT_ENABLED = os.environ.get("SECURITY_AUDIT_ENABLED", "true").lower() in ("1", "true", "yes")

# ── Event types ————————————————————————————————————————————————————————————————

EVT_JOB_CREATED      = "job_created"
EVT_JOB_CLAIMED      = "job_claimed"
EVT_JOB_COMPLETED    = "job_completed"
EVT_JOB_FAILED       = "job_failed"
EVT_UPLOAD_REJECTED  = "upload_rejected"
EVT_RATE_LIMITED     = "rate_limited"
EVT_QUOTA_EXCEEDED   = "quota_exceeded"
EVT_WORKER_AUTH_FAIL = "worker_auth_failed"
EVT_OWNER_MISMATCH   = "owner_mismatch"
EVT_INVALID_INPUT    = "invalid_input"


def audit(
    event_type: str,
    user_id: Optional[str] = None,
    job_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    trace_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Log a security event. Never logs secrets or sensitive data.
    DB write failures are non-fatal — always logs to Python logger.
    """
    safe_details = _sanitize_details(details or {})

    logger.info(
        "[audit] event=%s user_id=%s job_id=%s ip=%s trace_id=%s",
        event_type,
        user_id or "-",
        job_id or "-",
        _mask_ip(ip_address),
        trace_id or "-",
    )

    if not _AUDIT_ENABLED:
        return

    try:
        _write_to_db(
            event_type=event_type,
            user_id=user_id,
            job_id=job_id,
            ip_address=_mask_ip(ip_address),
            user_agent=_safe_ua(user_agent),
            trace_id=trace_id,
            details=safe_details,
        )
    except Exception as exc:
        logger.warning("[audit] db_write_failed event=%s error=%s", event_type, exc)


def _write_to_db(
    event_type: str,
    user_id: Optional[str],
    job_id: Optional[str],
    ip_address: Optional[str],
    user_agent: Optional[str],
    trace_id: Optional[str],
    details: Dict[str, Any],
) -> None:
    import json as _json
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO security_audit_log
                        (event_type, user_id, job_id, ip_address, user_agent, trace_id, details, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event_type, user_id, job_id, ip_address, user_agent,
                        trace_id, _json.dumps(details),
                        datetime.now(timezone.utc),
                    ),
                )
    finally:
        conn.close()


def _sanitize_details(details: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any keys that might contain secrets."""
    _FORBIDDEN_KEYS = {
        "password", "secret", "token", "api_key", "access_key",
        "private_key", "authorization", "cookie", "session",
    }
    return {
        k: v for k, v in details.items()
        if k.lower() not in _FORBIDDEN_KEYS
    }


def _mask_ip(ip: Optional[str]) -> Optional[str]:
    """Mask last octet of IPv4 or last segment of IPv6."""
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.xxx"
    # IPv6: mask last group
    parts6 = ip.split(":")
    if len(parts6) > 1:
        parts6[-1] = "xxxx"
        return ":".join(parts6)
    return ip


def _safe_ua(ua: Optional[str]) -> Optional[str]:
    """Truncate user-agent to prevent log injection."""
    if not ua:
        return None
    return ua[:200].replace("\n", " ").replace("\r", " ")
