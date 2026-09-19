"""
robokassa.py
============
Pure Robokassa integration logic for SONYA -- signature building/verification,
payment-URL construction, plan catalog. No FastAPI, no DB (mirrors
auth_security.py's role for the auth module).

Verified against the current official Robokassa documentation
(docs.robokassa.ru/ru/pay-interface, /ru/notifications-and-redirects,
/openapi/robokassa.yaml) at implementation time -- not older third-party
write-ups.

Test vs production mode is NOT a single global switch applied at
verification time: a payment's `is_test` flag is captured once, at
creation, and carried on the `payments` row (see payment_store.py). This
module always takes the relevant password as an explicit argument rather
than reading ROBOKASSA_TEST_MODE itself, so callers can never accidentally
verify a production callback with a test password or vice versa.

Recurring payments are intentionally NOT implemented here -- see the
project's payment plan notes: Robokassa's /Merchant/Recurring endpoint has
no test mode and requires separate merchant approval. First release is
one-time payment only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

ROBOKASSA_PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"


# ── Plan catalog (server-side source of truth for price/duration) ───────────

@dataclass(frozen=True)
class Plan:
    plan_id: str
    plan_type: str
    amount: Decimal
    duration_days: int
    description: str


PLAN_CATALOG: dict[str, Plan] = {
    "pro_30d": Plan(
        plan_id="pro_30d",
        plan_type="pro",
        amount=Decimal("500.00"),
        duration_days=30,
        description="SONYA Pro — подписка на 30 дней",
    ),
}


def get_plan(plan_id: str) -> Optional[Plan]:
    return PLAN_CATALOG.get(plan_id)


# ── Config ────────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def merchant_login() -> str:
    login = _env("ROBOKASSA_MERCHANT_LOGIN")
    if not login:
        raise RuntimeError("ROBOKASSA_MERCHANT_LOGIN not configured")
    return login


def is_test_mode() -> bool:
    return _env("ROBOKASSA_TEST_MODE", "false").lower() in ("1", "true", "yes")


def password_1(is_test: bool) -> str:
    name = "ROBOKASSA_TEST_PASSWORD_1" if is_test else "ROBOKASSA_PASSWORD_1"
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} not configured")
    return value


def password_2(is_test: bool) -> str:
    name = "ROBOKASSA_TEST_PASSWORD_2" if is_test else "ROBOKASSA_PASSWORD_2"
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} not configured")
    return value


def receipt_enabled() -> bool:
    return _env("ROBOKASSA_RECEIPT_ENABLED", "false").lower() in ("1", "true", "yes")


# ── Formatting ────────────────────────────────────────────────────────────

def format_out_sum(amount: Decimal) -> str:
    """Robokassa wants a plain decimal string, e.g. "500.00" -- only used
    when WE construct the outgoing payment request. Never used to
    re-format an incoming OutSum before verifying its signature (Robokassa
    itself may echo back a different number of decimal places, e.g.
    "500.000000" in production vs "500.00" in test mode)."""
    return format(amount, "f")


def build_receipt(plan: Plan) -> Optional[str]:
    """
    Build the Receipt JSON string for a plan, if fiscalization is enabled.
    Returns None when ROBOKASSA_RECEIPT_ENABLED is false (the default --
    see the payment plan notes: the seller is self-employed / NPD and the
    exact fiscalization scheme for this shop has not been confirmed with
    Robokassa yet, so nothing is sent until it is).

    When enabled, sno/tax/payment_method/payment_object come from env --
    never guessed here, since these are legally significant values.
    """
    if not receipt_enabled():
        return None

    import json

    tax = _env("ROBOKASSA_RECEIPT_TAX")
    if not tax:
        raise RuntimeError("ROBOKASSA_RECEIPT_TAX not configured but ROBOKASSA_RECEIPT_ENABLED=true")

    item: dict = {
        "name": plan.description[:128],
        "quantity": 1,
        "sum": float(plan.amount),
        "tax": tax,
    }
    payment_method = _env("ROBOKASSA_RECEIPT_PAYMENT_METHOD")
    if payment_method:
        item["payment_method"] = payment_method
    payment_object = _env("ROBOKASSA_RECEIPT_PAYMENT_OBJECT")
    if payment_object:
        item["payment_object"] = payment_object

    receipt: dict = {"items": [item]}
    sno = _env("ROBOKASSA_RECEIPT_SNO")
    if sno:
        receipt["sno"] = sno

    return json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))


# ── Signatures ────────────────────────────────────────────────────────────

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_payment_signature(out_sum: str, inv_id: int, receipt_json: Optional[str], password1: str) -> str:
    """MerchantLogin:OutSum:InvId[:Receipt]:Password#1 -- Receipt segment is
    included only when a Receipt is actually being sent (no placeholder
    slot when fiscalization is off)."""
    parts = [merchant_login(), out_sum, str(inv_id)]
    if receipt_json is not None:
        parts.append(receipt_json)
    parts.append(password1)
    return _md5(":".join(parts))


def build_payment_url(
    *,
    out_sum: str,
    inv_id: int,
    description: str,
    receipt_json: Optional[str],
    is_test: bool,
) -> str:
    signature = build_payment_signature(out_sum, inv_id, receipt_json, password_1(is_test))
    params = {
        "MerchantLogin": merchant_login(),
        "OutSum": out_sum,
        "InvId": str(inv_id),
        "Description": description,
        "SignatureValue": signature,
        "Culture": "ru",
    }
    if receipt_json is not None:
        params["Receipt"] = receipt_json
    if is_test:
        params["IsTest"] = "1"
    return f"{ROBOKASSA_PAYMENT_URL}?{urlencode(params)}"


def verify_result_signature(raw_out_sum: str, inv_id: int, signature: str, is_test: bool) -> bool:
    """OutSum:InvId:Password#2 -- verified against the RAW OutSum string as
    received from Robokassa, never a re-formatted copy of it (production
    and test mode can echo different decimal precision for the same
    amount). Constant-time comparison."""
    expected = _md5(f"{raw_out_sum}:{inv_id}:{password_2(is_test)}")
    return hmac.compare_digest(expected.lower(), signature.lower())
