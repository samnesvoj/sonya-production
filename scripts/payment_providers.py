"""
payment_providers.py
====================
The only place that knows which payment provider collects money. Plans,
prices and entitlements are provider independent (scripts/pricing.py,
scripts/entitlements.py); a provider's whole job is:

  1. create_checkout(payment, plan) -> URL the browser is sent to, for a
     payments row whose plan_id/amount were already fixed by the server
     catalog at checkout (payment_routes.checkout);
  2. in its own webhook route, prove the notification is authentic and
     that it is about one of ITS payments, then call
     payment_store.process_successful_payment(invoice_id, amount, raw),
     which activates exactly the plan snapshotted on that payment.

Active provider = env PAYMENT_PROVIDER. Unset (default) -> no provider:
checkout answers "payment unavailable" before any payment row is created.

Providers:
  * "robokassa" -- LEGACY. SONYA no longer takes payments through it; kept
    so a payment created through it can still be confirmed (its ResultURL
    route stays in payment_routes.py) and usable only by explicit opt-in.
  * Самозанятые.рф -- the production provider. NOT implemented: the
    repository has no API contract for it (endpoints, auth, request /
    response / webhook payloads, signature scheme, test mode). Add a class
    here implementing PaymentProvider and its webhook route once that
    contract is available -- nothing else needs to change.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Protocol

from scripts.pricing import Plan


class PaymentProvider(Protocol):
    name: str

    def is_test_mode(self) -> bool: ...

    def create_checkout(self, payment: Dict[str, Any], plan: Plan) -> str:
        """Return the URL to send the browser to. Raise RuntimeError on
        missing/invalid configuration (never leak which setting)."""
        ...


class RobokassaProvider:
    """LEGACY -- see module docstring."""

    name = "robokassa"

    def is_test_mode(self) -> bool:
        from scripts import robokassa
        return robokassa.is_test_mode()

    def create_checkout(self, payment: Dict[str, Any], plan: Plan) -> str:
        from scripts import robokassa
        return robokassa.build_payment_url(
            out_sum=robokassa.format_out_sum(plan.amount),
            inv_id=payment["invoice_id"],
            description=plan.description,
            receipt_json=robokassa.build_receipt(plan),
            is_test=payment["is_test"],
        )


_PROVIDERS = {RobokassaProvider.name: RobokassaProvider}


def get_checkout_provider() -> Optional[PaymentProvider]:
    """Provider for NEW checkouts, or None when none is configured."""
    name = os.environ.get("PAYMENT_PROVIDER", "").strip().lower()
    cls = _PROVIDERS.get(name)
    return cls() if cls else None
