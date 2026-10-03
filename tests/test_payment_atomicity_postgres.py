"""
Real PostgreSQL integration test for scripts.payment_store.process_successful_payment.

A mocked test can prove the Python-level branching (pending -> paid, etc.)
but cannot prove that N genuinely concurrent webhook calls for the SAME
invoice_id are serialized so that exactly one of them extends the
subscription -- that guarantee comes entirely from `SELECT ... FOR UPDATE`
inside a real Postgres transaction (see scripts/payment_store.py). Same
pattern as tests/test_job_idempotency_postgres.py.

Skipped automatically unless DATABASE_URL is set.
Local run:
    createdb sonya_payment_test
    DATABASE_URL=postgresql://localhost/sonya_payment_test python scripts/run_migrations.py
    DATABASE_URL=postgresql://localhost/sonya_payment_test pytest tests/test_payment_atomicity_postgres.py -v
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres payment atomicity test skipped locally",
)


@pytest.fixture()
def stores():
    from scripts import auth_store, payment_store
    return auth_store, payment_store


def _mk_user(auth_store, email_suffix):
    import uuid
    return auth_store.create_user(f"payment-test-{uuid.uuid4()}-{email_suffix}@example.com")


def test_concurrent_valid_callbacks_activate_exactly_once(stores):
    auth_store, payment_store = stores
    user = _mk_user(auth_store, "concurrent")
    payment = payment_store.create_pending_payment(
        user_id=user["id"], plan_id="cut_pro", amount=Decimal("2690.00"), is_test=True,
        plan_type="pro", duration_days=30, provider="robokassa",
    )
    invoice_id = payment["invoice_id"]
    n = 8

    def attempt(_i):
        return payment_store.process_successful_payment(invoice_id, Decimal("2690.00"), {"attempt": _i})

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    activated = [r for r in results if r["result"] == "activated"]
    already = [r for r in results if r["result"] == "already_processed"]

    assert len(activated) == 1, f"expected exactly one activation, got {len(activated)}: {results}"
    assert len(already) == n - 1

    updated_user = auth_store.get_user_by_id(user["id"])
    assert updated_user["plan_type"] == "pro"
    assert updated_user["plan_status"] == "active"
    # Extended by exactly one 30-day period, not N periods.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    delta = updated_user["plan_active_until"] - now
    assert timedelta(days=29) < delta < timedelta(days=31), f"unexpected extension: {delta}"


def test_amount_mismatch_does_not_activate_or_touch_subscription(stores):
    auth_store, payment_store = stores
    user = _mk_user(auth_store, "mismatch")
    payment = payment_store.create_pending_payment(
        user_id=user["id"], plan_id="cut_pro", amount=Decimal("2690.00"), is_test=True,
        plan_type="pro", duration_days=30, provider="robokassa",
    )
    invoice_id = payment["invoice_id"]

    outcome = payment_store.process_successful_payment(invoice_id, Decimal("1.00"), {})
    assert outcome["result"] == "amount_mismatch"

    updated_user = auth_store.get_user_by_id(user["id"])
    assert updated_user["plan_type"] == "free"
    assert updated_user["plan_active_until"] is None

    unchanged_payment = payment_store.get_payment_by_invoice_id(invoice_id)
    assert unchanged_payment["status"] == "pending"


def test_renewal_extends_from_existing_active_period_not_from_now(stores):
    """A second successful payment while a Pro period is still active
    should stack on top of the remaining time, not reset it to 30 days
    from today."""
    auth_store, payment_store = stores
    user = _mk_user(auth_store, "renewal")

    first = payment_store.create_pending_payment(
        user_id=user["id"], plan_id="cut_pro", amount=Decimal("2690.00"), is_test=True,
        plan_type="pro", duration_days=30, provider="robokassa",
    )
    payment_store.process_successful_payment(first["invoice_id"], Decimal("2690.00"), {})
    after_first = auth_store.get_user_by_id(user["id"])
    first_until = after_first["plan_active_until"]

    second = payment_store.create_pending_payment(
        user_id=user["id"], plan_id="cut_pro", amount=Decimal("2690.00"), is_test=True,
        plan_type="pro", duration_days=30, provider="robokassa",
    )
    payment_store.process_successful_payment(second["invoice_id"], Decimal("2690.00"), {})
    after_second = auth_store.get_user_by_id(user["id"])
    second_until = after_second["plan_active_until"]

    from datetime import timedelta
    assert (second_until - first_until) > timedelta(days=29)


def test_activation_uses_payment_snapshot_not_live_catalog(stores):
    """plan_type/duration_days are captured on the payment row at checkout
    time (create_pending_payment). If PLAN_CATALOG is edited afterwards
    (e.g. Pro's duration changed from 30 to 14 days) before the webhook
    confirms THIS payment, activation must still honor what was actually
    paid for -- 30 days, matching this payment's own snapshot -- not
    whatever the catalog says at the moment the webhook happens to arrive."""
    auth_store, payment_store = stores
    user = _mk_user(auth_store, "catalog-snapshot")

    # Checkout happens while the catalog still says 30 days.
    payment = payment_store.create_pending_payment(
        user_id=user["id"], plan_id="cut_pro", amount=Decimal("2690.00"), is_test=True,
        plan_type="pro", duration_days=30, provider="robokassa",
    )

    # Catalog changes before the webhook arrives -- payment_store doesn't
    # import PLAN_CATALOG at all anymore, so this is really just documenting
    # the scenario; the assertion below is what actually proves immunity.
    from scripts import pricing
    from dataclasses import replace
    original_plan = pricing.PLAN_CATALOG["cut_pro"]
    pricing.PLAN_CATALOG["cut_pro"] = replace(original_plan, duration_days=14)
    try:
        outcome = payment_store.process_successful_payment(payment["invoice_id"], Decimal("2690.00"), {})
    finally:
        pricing.PLAN_CATALOG["cut_pro"] = original_plan

    assert outcome["result"] == "activated"

    from datetime import datetime, timedelta, timezone
    updated_user = auth_store.get_user_by_id(user["id"])
    delta = updated_user["plan_active_until"] - datetime.now(timezone.utc)
    assert timedelta(days=29) < delta < timedelta(days=31), (
        f"activation used the live (edited) catalog instead of this payment's own snapshot: {delta}"
    )
