"""
payment_store.py
=================
PostgreSQL data access layer for Robokassa payments (migration 010).

All functions open/close their own connection (matches auth_store.py style
-- low-QPS path, not the hot job-processing path).

process_successful_payment() is the ONLY place that transitions a payment
to 'paid' and extends a subscription -- and it does both in a single DB
transaction with a row lock, so a crash between "mark paid" and "extend
subscription" cannot happen (Postgres rolls back the whole thing, and a
retried Robokassa callback safely redoes it from scratch), and two
concurrent callbacks for the same invoice_id serialize on the row lock
instead of racing.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_pending_payment(
    user_id: str, plan_id: str, amount: Decimal, is_test: bool,
    plan_type: str, duration_days: int,
) -> Dict[str, Any]:
    """
    plan_type/duration_days are a snapshot of PLAN_CATALOG[plan_id] at
    checkout time (caller passes them in -- this module has no PLAN_CATALOG
    dependency), exactly like `amount`. process_successful_payment() below
    reads them back from this row, never from a fresh catalog lookup, so a
    later catalog change can't alter the terms of an already-created payment.
    """
    payment_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payments
                        (id, user_id, plan_id, amount, is_test, plan_type, duration_days, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (payment_id, user_id, plan_id, amount, is_test, plan_type, duration_days, _now(), _now()),
                )
                row = cur.fetchone()
    finally:
        conn.close()
    return dict(row)


def get_payment_by_invoice_id(invoice_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE invoice_id = %s", (invoice_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def process_successful_payment(invoice_id: int, out_sum: Decimal, raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Atomically: lock the payment row, verify it's pending and the amount
    matches, mark it paid, and extend the user's subscription -- all in one
    transaction. Returns {"result": ..., "payment": {...}} where result is
    one of: "activated", "already_processed", "not_found", "invalid_state",
    "amount_mismatch".

    Caller (payment_routes.py) must call this ONLY after verify_result_signature
    has already passed -- this function does not check the signature.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM payments WHERE invoice_id = %s FOR UPDATE",
                    (invoice_id,),
                )
                payment = cur.fetchone()
                if not payment:
                    return {"result": "not_found"}
                payment = dict(payment)

                if payment["status"] == "paid":
                    return {"result": "already_processed", "payment": payment}
                if payment["status"] != "pending":
                    return {"result": "invalid_state", "payment": payment}
                if payment["amount"] != out_sum:
                    logger.warning(
                        "[payment] amount_mismatch invoice_id=%s expected=%s received=%s",
                        invoice_id, payment["amount"], out_sum,
                    )
                    return {"result": "amount_mismatch", "payment": payment}

                cur.execute(
                    """
                    UPDATE payments
                    SET status = 'paid', paid_at = %s, result_received_at = %s,
                        raw_result_params = %s, updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (_now(), _now(), json.dumps(raw_params, default=str), _now(), payment["id"]),
                )
                updated = dict(cur.fetchone())

                # Terms come from THIS payment's own snapshot
                # (plan_type/duration_days, set at checkout time) -- never
                # a fresh PLAN_CATALOG lookup, so a catalog change made
                # after checkout can't alter what an already-pending
                # payment activates.
                cur.execute(
                    """
                    UPDATE users
                    SET plan_type = %s,
                        plan_status = 'active',
                        plan_active_until = GREATEST(NOW(), COALESCE(plan_active_until, NOW()))
                                             + (%s || ' days')::interval,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (payment["plan_type"], payment["duration_days"], _now(), payment["user_id"]),
                )
            return {"result": "activated", "payment": updated}
    finally:
        conn.close()
