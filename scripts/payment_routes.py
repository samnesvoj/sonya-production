"""
payment_routes.py
==================
Billing endpoints for SONYA.

  POST /api/billing/checkout                  (browser, cookie auth) -- provider independent
  GET  /api/billing/payment-status             (browser, cookie auth) -- provider independent
  GET  /api/billing/checkout-availability      (public) -- is any provider configured
  POST /api/billing/robokassa/result           LEGACY Robokassa ResultURL (server-to-server)

Checkout takes only a plan_id: price and terms come from the server catalog
(scripts/pricing.py) and are snapshotted on the payments row; the active
provider (scripts/payment_providers.py, env PAYMENT_PROVIDER) only turns
that row into a redirect URL. A provider's webhook confirms the payment via
payment_store.process_successful_payment(), which activates exactly the
snapshotted plan. No provider configured -> checkout returns 503
payment_unavailable before creating anything.

Same pattern as scripts/auth_routes.py: a plain APIRouter() with no
prefix, each path spelled out in full ("/api/...") in the decorator, and
included in the app with app.include_router(payment_router) -- no
prefix= argument. This keeps the actual route exactly
"/api/billing/robokassa/result", never "/billing/..." or "/api/api/...".

The return pages (payment/success.html, payment/fail.html) are static and
only ever call GET /api/billing/payment-status -- they never hit an endpoint
that can change payment/subscription state. Only a provider's verified
server-to-server notification confirms a payment; see
scripts/payment_store.py::process_successful_payment.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from scripts import entitlements, payment_store
from scripts.payment_providers import RobokassaProvider, get_checkout_provider
from scripts.pricing import get_plan
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

    # A running period for the same mode that still has operations left
    # would be closed by the new purchase (see
    # payment_store._activate_subscription) -- refuse instead of silently
    # burning what the user already paid for. Exhausted/expired periods
    # don't block buying the next one.
    try:
        current = next(
            (s for s in entitlements.active_subscriptions(entitlements.get_user_subscriptions(str(user["id"])))
             if s["plan_mode"] == plan.plan_mode and s["ops_used"] < s["ops_limit"]),
            None,
        )
    except Exception as exc:
        logger.error(
            "[payment] db_error operation=get_user_subscriptions user_id=%s trace_id=%s error_type=%s",
            user["id"], trace_id, type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "internal_error", "trace_id": trace_id},
        )
    if current is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "subscription_active", "trace_id": trace_id,
                    "subscription": entitlements.serialize_subscription(current)},
        )

    provider = get_checkout_provider()
    if provider is None:
        # No acquiring connected yet (Самозанятые.рф integration pending,
        # Robokassa retired) -- refuse before creating a payment row the
        # user could never pay.
        logger.warning("[payment] checkout_unavailable no_provider user_id=%s plan_id=%s trace_id=%s",
                       user["id"], plan.plan_id, trace_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "payment_unavailable", "trace_id": trace_id},
        )

    is_test = provider.is_test_mode()

    try:
        payment = payment_store.create_pending_payment(
            user_id=str(user["id"]), plan_id=plan.plan_id, amount=plan.amount, is_test=is_test,
            plan_type=plan.plan_type, duration_days=plan.duration_days,
            plan_mode=plan.plan_mode, ops_limit=plan.ops_limit, max_source_sec=plan.max_source_sec,
            provider=provider.name,
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

    try:
        redirect_url = provider.create_checkout(payment, plan)
    except RuntimeError as exc:
        # Missing/misconfigured provider credentials -- never leak which
        # env var, just fail safe with a trace_id for server-side logs.
        logger.error("[payment] checkout_config_error trace_id=%s: %s", trace_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "payment_unavailable", "trace_id": trace_id},
        )

    logger.info(
        "[payment] checkout_created provider=%s user_id=%s invoice_id=%s plan_id=%s is_test=%s trace_id=%s",
        provider.name, user["id"], payment["invoice_id"], plan.plan_id, is_test, trace_id,
    )

    return {"invoice_id": payment["invoice_id"], "redirect_url": redirect_url}


# ── POST /api/billing/robokassa/result (LEGACY Robokassa ResultURL) ─────────
# Kept only so a payment created through Robokassa can still be confirmed.
# It refuses any payment whose provider isn't "robokassa".

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

    # Imported here, not at module level: only this legacy route needs
    # Robokassa -- checkout and the tariff system never import it.
    from scripts.robokassa import verify_result_signature_for_either_mode

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

    if (payment.get("provider") or RobokassaProvider.name) != RobokassaProvider.name:
        # invoice_id is a shared order number -- a Robokassa-signed callback
        # must never confirm another provider's payment.
        logger.warning("[payment] result_wrong_provider invoice_id=%s provider=%s trace_id=%s",
                       InvId, payment.get("provider"), trace_id)
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

# ── GET /api/billing/checkout-availability ───────────────────────────────────
# Public (guests browse plans too). Lets the plan picker show its pay CTA as
# "Оплата скоро будет доступна" while no provider is configured, and enable it
# by itself once PAYMENT_PROVIDER is set -- no frontend release needed.

@router.get("/api/billing/checkout-availability")
async def checkout_availability():
    return {"available": get_checkout_provider() is not None}


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

    # The subscription this payment opened (if paid and it's a new-style
    # plan) -- lets success.html show what was actually activated.
    subscription = None
    if payment["status"] == "paid" and payment.get("plan_mode"):
        try:
            subscription = next(
                (entitlements.serialize_subscription(s)
                 for s in entitlements.get_user_subscriptions(str(user["id"]))
                 if str(s.get("payment_id")) == str(payment["id"])),
                None,
            )
        except Exception as exc:
            logger.warning("[payment] payment_status_subscription_lookup_failed trace_id=%s error_type=%s",
                           trace_id, type(exc).__name__)

    return {
        "status": payment["status"],
        "plan_id": payment["plan_id"],
        "plan_type": user["plan_type"],
        "plan_active_until": subscription["period_end"] if subscription else _iso(user.get("plan_active_until")),
        "subscription": subscription,
    }
