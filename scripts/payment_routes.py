"""
payment_routes.py
==================
Robokassa checkout + webhook endpoints for SONYA.

  POST /api/billing/checkout                  (browser, cookie auth)
  POST /api/billing/robokassa/result           (Robokassa server-to-server, no auth)
  GET  /api/billing/payment-status             (browser, cookie auth)

Same pattern as scripts/auth_routes.py: a plain APIRouter() with no
prefix, each path spelled out in full ("/api/...") in the decorator, and
included in the app with app.include_router(payment_router) -- no
prefix= argument. This keeps the actual route exactly
"/api/billing/robokassa/result", never "/billing/..." or "/api/api/...".

SuccessURL/FailURL (payment/success.html, payment/fail.html) are static
pages that only ever call GET /api/billing/payment-status -- they never
hit an endpoint that can change payment/subscription state. ResultURL is
the only source of truth for a completed payment; see
scripts/payment_store.py::process_successful_payment.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from scripts import payment_store
from scripts.robokassa import (
    build_payment_url,
    build_receipt,
    format_out_sum,
    get_plan,
    is_test_mode,
    verify_result_signature_for_either_mode,
)
from scripts.security import get_current_user, new_trace_id, verify_browser_origin

logger = logging.getLogger(__name__)

router = APIRouter()


class CheckoutRequest(BaseModel):
    plan_id: str


def _iso(dt):
    return dt.isoformat() if dt else None


# ── POST /api/billing/checkout ───────────────────────────────────────────────

@router.post("/api/billing/checkout")
async def checkout(
    body: CheckoutRequest,
    user: dict = Depends(get_current_user),
    _origin: None = Depends(verify_browser_origin),
):
    trace_id = new_trace_id()

    plan = get_plan(body.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_plan", "trace_id": trace_id},
        )

    is_test = is_test_mode()

    try:
        payment = payment_store.create_pending_payment(
            user_id=str(user["id"]), plan_id=plan.plan_id, amount=plan.amount, is_test=is_test,
            plan_type=plan.plan_type, duration_days=plan.duration_days,
        )
    except Exception as exc:
        logger.error(
            "[payment] db_error operation=create_pending_payment user_id=%s trace_id=%s error_type=%s",
            user["id"], trace_id, type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "internal_error", "trace_id": trace_id},
        )

    out_sum = format_out_sum(plan.amount)
    receipt_json = build_receipt(plan)

    try:
        redirect_url = build_payment_url(
            out_sum=out_sum,
            inv_id=payment["invoice_id"],
            description=plan.description,
            receipt_json=receipt_json,
            is_test=is_test,
        )
    except RuntimeError as exc:
        # Missing/misconfigured Robokassa credentials -- never leak which
        # env var, just fail safe with a trace_id for server-side logs.
        logger.error("[payment] checkout_config_error trace_id=%s: %s", trace_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "payment_unavailable", "trace_id": trace_id},
        )

    logger.info(
        "[payment] checkout_created user_id=%s invoice_id=%s plan_id=%s is_test=%s trace_id=%s",
        user["id"], payment["invoice_id"], plan.plan_id, is_test, trace_id,
    )

    return {"invoice_id": payment["invoice_id"], "redirect_url": redirect_url}


# ── POST /api/billing/robokassa/result (ResultURL) ───────────────────────────

@router.post("/api/billing/robokassa/result")
async def robokassa_result(
    request: Request,
    OutSum: str = Form(...),
    InvId: int = Form(...),
    SignatureValue: str = Form(...),
):
    """
    Order matters here and is deliberate (see the review notes this fixes):
      1. Verify SignatureValue first, against BOTH production and test
         Password#2 -- no database call at all yet. A request with no
         valid signature for either mode is rejected immediately, so
         garbage/scanning traffic against this public, unauthenticated
         endpoint never reaches Postgres.
      2. Only once a signature validates, look up the payment by InvId and
         confirm its stored is_test flag matches whichever password
         actually matched (a signature valid under the TEST password must
         never activate a payment created in production mode, or vice
         versa).
      3. Only then compare Decimal(OutSum) against the stored amount and
         atomically process the payment.
    """
    trace_id = new_trace_id()

    matched_is_test = verify_result_signature_for_either_mode(OutSum, InvId, SignatureValue)
    if matched_is_test is None:
        logger.warning("[payment] result_bad_signature invoice_id=%s trace_id=%s", InvId, trace_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature")

    try:
        out_sum_decimal = Decimal(OutSum)
    except InvalidOperation:
        logger.warning("[payment] result_bad_out_sum invoice_id=%s out_sum=%r trace_id=%s", InvId, OutSum, trace_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid amount")

    try:
        payment = payment_store.get_payment_by_invoice_id(InvId)
    except Exception as exc:
        logger.error(
            "[payment] db_error operation=get_payment_by_invoice_id invoice_id=%s trace_id=%s error_type=%s",
            InvId, trace_id, type(exc).__name__,
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                             detail={"error": "internal_error", "trace_id": trace_id})

    if payment is None:
        logger.warning("[payment] result_unknown_invoice invoice_id=%s trace_id=%s", InvId, trace_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown invoice")

    if payment["is_test"] != matched_is_test:
        logger.warning(
            "[payment] result_mode_mismatch invoice_id=%s trace_id=%s expected_is_test=%s matched_is_test=%s",
            InvId, trace_id, payment["is_test"], matched_is_test,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature")

    raw_params = dict((await request.form()))
    try:
        outcome = payment_store.process_successful_payment(InvId, out_sum_decimal, raw_params)
    except Exception as exc:
        logger.error(
            "[payment] db_error operation=process_successful_payment invoice_id=%s trace_id=%s error_type=%s",
            InvId, trace_id, type(exc).__name__,
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                             detail={"error": "internal_error", "trace_id": trace_id})

    if outcome["result"] in ("activated", "already_processed"):
        if outcome["result"] == "activated":
            logger.info("[payment] result_activated invoice_id=%s trace_id=%s", InvId, trace_id)
        return PlainTextResponse(f"OK{InvId}")

    logger.error(
        "[payment] result_rejected invoice_id=%s outcome=%s trace_id=%s",
        InvId, outcome["result"], trace_id,
    )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=outcome["result"])


# ── GET /api/billing/payment-status ──────────────────────────────────────────

@router.get("/api/billing/payment-status")
async def payment_status(
    invoice_id: int,
    user: dict = Depends(get_current_user),
):
    trace_id = new_trace_id()
    payment = payment_store.get_payment_by_invoice_id(invoice_id)
    if payment is None or payment["user_id"] != user["id"]:
        # Same 404-not-403 pattern as security.py::assert_job_owner -- never
        # confirm/deny existence of another user's payment.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found", "trace_id": trace_id})

    return {
        "status": payment["status"],
        "plan_type": user["plan_type"],
        "plan_active_until": _iso(user.get("plan_active_until")),
    }
