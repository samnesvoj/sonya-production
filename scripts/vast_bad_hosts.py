"""
vast_bad_hosts.py
==================
PostgreSQL-backed blacklist/cooldown for slow-starting vast.ai hosts.

Table: vast_bad_hosts (migration scripts/migrations/007_vast_bad_hosts.sql)

Part of the production Vast startup SLA:
  - gpu_dispatcher.cleanup_stale_gpu_requests() calls add_bad_host() whenever
    a worker fails to report in within VAST_STARTUP_TIMEOUT_SEC.
  - gpu_orchestrator._vast_search_offers() calls get_active_bad_hosts() before
    picking an offer, rejecting any offer whose host_id/machine_id/ip matches
    an active row (reason=slow_startup_blacklist).

Fail-open by design: any DB error here is logged and swallowed rather than
raised, so a blacklist-table outage never blocks the whole GPU queue.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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


def add_bad_host(
    host_id: Optional[str] = None,
    machine_id: Optional[str] = None,
    ip: Optional[str] = None,
    offer_id: Optional[str] = None,
    reason: str = "slow_startup",
    cooldown_minutes: int = 60,
) -> None:
    """
    Insert a cooldown row for a host/machine/ip that failed the startup SLA.

    Never raises — a blacklist-insert failure must not prevent the job from
    being requeued/failed; logs a warning instead.
    """
    if not (host_id or machine_id or ip):
        logger.warning(
            "vast_bad_hosts_skip reason=no_identifiers offer_id=%s", offer_id
        )
        return
    try:
        conn = _get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO vast_bad_hosts
                            (host_id, machine_id, ip, offer_id, reason, blocked_until, created_at)
                        VALUES (%s, %s, %s, %s, %s, NOW() + (%s || ' minutes')::INTERVAL, NOW())
                        """,
                        (host_id, machine_id, ip, offer_id, reason, str(cooldown_minutes)),
                    )
        finally:
            conn.close()
        logger.warning(
            "vast_bad_host_added host_id=%s machine_id=%s ip=%s offer_id=%s "
            "reason=%s cooldown_min=%d",
            host_id, machine_id, ip, offer_id, reason, cooldown_minutes,
        )
    except Exception as exc:
        logger.warning("vast_bad_hosts_insert_failed exc=%s", exc)


def get_active_bad_hosts() -> List[Dict[str, Any]]:
    """
    Return all currently-active (non-expired) blacklist rows.

    Fail-open: on any DB error (unreachable DB, missing table, etc.) returns
    an empty list rather than raising, so offer search still proceeds.
    """
    try:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT host_id, machine_id, ip, offer_id, reason, blocked_until
                    FROM vast_bad_hosts
                    WHERE blocked_until > NOW()
                    """
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("vast_bad_hosts_query_failed exc=%s", exc)
        return []


def cleanup_expired(older_than_days: int = 7) -> int:
    """Optional housekeeping: delete long-expired rows. Never raises."""
    try:
        conn = _get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM vast_bad_hosts
                        WHERE blocked_until < NOW() - (%s || ' days')::INTERVAL
                        """,
                        (str(older_than_days),),
                    )
                    return cur.rowcount
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("vast_bad_hosts_cleanup_failed exc=%s", exc)
        return 0
