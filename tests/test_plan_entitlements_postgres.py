"""
Real PostgreSQL integration tests for per-plan entitlements (migration 016).

What a mocked test cannot prove:
  * the Robokassa callback activates exactly the plan that was paid for
    (from the payment's own snapshot), and never the legacy "pro";
  * a subscription operation is debited atomically with the job insert --
    never twice for an idempotent replay, never past ops_limit under
    concurrency, never for an expired period or another user's period,
    and rolled back if the job insert fails.

Skipped automatically unless DATABASE_URL is set (point it at a scratch
database, run scripts/run_migrations.py, then this file).
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres entitlement test skipped locally",
)


def _sql(query, params=(), fetch=False):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()] if fetch else None
    finally:
        conn.close()


def _mk_user(free_video_used=1, free_video_limit=1):
    from scripts import auth_store
    user = auth_store.create_user(f"plan-test-{uuid.uuid4()}@example.com")
    _sql("UPDATE users SET free_video_used = %s, free_video_limit = %s WHERE id = %s",
         (free_video_used, free_video_limit, user["id"]))
    return auth_store.get_user_by_id(user["id"])


def _checkout(user_id, plan_id):
    """Same calls payment_routes.checkout makes -- terms from the catalog."""
    from scripts import payment_store
    from scripts.pricing import get_plan
    plan = get_plan(plan_id)
    return payment_store.create_pending_payment(
        user_id=str(user_id), plan_id=plan.plan_id, amount=plan.amount, is_test=True,
        plan_type=plan.plan_type, duration_days=plan.duration_days,
        plan_mode=plan.plan_mode, ops_limit=plan.ops_limit, max_source_sec=plan.max_source_sec,
        provider="test-provider",
    )


def _pay(payment):
    from scripts import payment_store
    return payment_store.process_successful_payment(payment["invoice_id"], payment["amount"], {})


def _subs(user_id):
    from scripts import entitlements
    return entitlements.get_user_subscriptions(str(user_id))


def _job_kwargs(user_id, subscription_id, **overrides):
    kwargs = dict(
        job_id=str(uuid.uuid4()), user_id=str(user_id), mode="virality", params={},
        s3_input_key=f"users/{user_id}/jobs/{uuid.uuid4()}/virality/input/file.mp4",
        idempotency_key=None, idempotency_fingerprint=None, queue_priority=0,
        bypass_quota=False, subscription_id=subscription_id,
    )
    kwargs.update(overrides)
    return kwargs


# ── Robokassa callback -> activation ─────────────────────────────────────

@pytest.mark.parametrize("plan_id,mode,ops,max_sec", [
    ("cut_start", "cut", 10, 3600), ("cut_pro", "cut", 20, 7200), ("cut_studio", "cut", 30, 10800),
    ("trailer_start", "trailer", 8, 5400), ("trailer_pro", "trailer", 12, 7200),
    ("trailer_studio", "trailer", 24, 10800), ("streamer_start", "streamer", 10, 14400),
])
def test_callback_activates_exactly_the_purchased_plan(plan_id, mode, ops, max_sec):
    user = _mk_user()
    payment = _checkout(user["id"], plan_id)
    outcome = _pay(payment)

    assert outcome["result"] == "activated"
    [sub] = _subs(user["id"])
    assert (sub["plan_id"], sub["plan_mode"], sub["ops_limit"], sub["max_source_sec"]) == (plan_id, mode, ops, max_sec)
    assert sub["ops_used"] == 0
    assert str(sub["payment_id"]) == str(payment["id"])
    assert timedelta(days=29, hours=23) < sub["period_end"] - sub["period_start"] <= timedelta(days=30)
    # The core bug this replaces: paying no longer grants the legacy
    # unlimited, all-modes "pro".
    [row] = _sql("SELECT plan_type, plan_active_until FROM users WHERE id = %s", (user["id"],), fetch=True)
    assert row["plan_type"] == "free" and row["plan_active_until"] is None


def test_activation_uses_payment_snapshot_not_current_catalog():
    """Catalog edited between checkout and callback -> the user still gets
    the terms they were shown and paid for."""
    from scripts import pricing
    user = _mk_user()
    payment = _checkout(user["id"], "cut_start")
    original = pricing.PLAN_CATALOG["cut_start"]
    from dataclasses import replace
    pricing.PLAN_CATALOG["cut_start"] = replace(original, ops_limit=1, max_source_sec=60)
    try:
        _pay(payment)
    finally:
        pricing.PLAN_CATALOG["cut_start"] = original
    [sub] = _subs(user["id"])
    assert (sub["ops_limit"], sub["max_source_sec"]) == (10, 3600)


def test_replayed_callback_does_not_open_a_second_period():
    user = _mk_user()
    payment = _checkout(user["id"], "trailer_pro")
    assert _pay(payment)["result"] == "activated"
    assert _pay(payment)["result"] == "already_processed"
    assert len(_subs(user["id"])) == 1


def test_amount_mismatch_activates_nothing():
    from decimal import Decimal
    from scripts import payment_store
    user = _mk_user()
    payment = _checkout(user["id"], "cut_studio")
    outcome = payment_store.process_successful_payment(payment["invoice_id"], Decimal("1.00"), {})
    assert outcome["result"] == "amount_mismatch"
    assert _subs(user["id"]) == []


def test_legacy_pending_payment_still_activates_legacy_pro():
    """A pro_30d payment created before migration 016 (no plan_mode) must
    still give the buyer what they paid for."""
    from decimal import Decimal
    from scripts import payment_store
    user = _mk_user()
    payment = payment_store.create_pending_payment(
        user_id=str(user["id"]), plan_id="pro_30d", amount=Decimal("500.00"), is_test=True,
        plan_type="pro", duration_days=30, provider="robokassa",
    )
    assert _pay(payment)["result"] == "activated"
    [row] = _sql("SELECT plan_type, plan_active_until FROM users WHERE id = %s", (user["id"],), fetch=True)
    assert row["plan_type"] == "pro" and row["plan_active_until"] is not None
    assert _subs(user["id"]) == []


def test_new_purchase_for_same_mode_closes_previous_period():
    user = _mk_user()
    _pay(_checkout(user["id"], "cut_start"))
    _pay(_checkout(user["id"], "cut_studio"))
    from scripts import entitlements
    active = entitlements.active_subscriptions(_subs(user["id"]))
    assert [s["plan_id"] for s in active] == ["cut_studio"]
    assert len(_subs(user["id"])) == 2  # history kept


def test_plans_for_different_modes_coexist():
    user = _mk_user()
    _pay(_checkout(user["id"], "cut_pro"))
    _pay(_checkout(user["id"], "streamer_start"))
    from scripts import entitlements
    active = entitlements.active_subscriptions(_subs(user["id"]))
    assert sorted(s["plan_mode"] for s in active) == ["cut", "streamer"]


# ── Atomic debit in create_job_with_quota ────────────────────────────────

def _active_sub(plan_id="cut_start"):
    user = _mk_user()
    _pay(_checkout(user["id"], plan_id))
    [sub] = _subs(user["id"])
    return user, sub


def test_subscription_job_debits_exactly_one_operation_and_records_it():
    from scripts import prod_job_store
    user, sub = _active_sub()
    res = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], str(sub["id"])))
    assert res["outcome"] == "created"
    assert str(res["job"]["charged_subscription_id"]) == str(sub["id"])
    [after] = _subs(user["id"])
    assert after["ops_used"] == 1
    # Free quota is untouched by a subscription job.
    [row] = _sql("SELECT free_video_used FROM users WHERE id = %s", (user["id"],), fetch=True)
    assert row["free_video_used"] == 1


def test_operation_past_limit_is_refused_without_creating_a_job():
    """cut_start = 10 operations: the 11th is refused."""
    from scripts import prod_job_store
    user, sub = _active_sub("cut_start")
    outcomes = [prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], str(sub["id"])))["outcome"]
                for _ in range(11)]
    assert outcomes == ["created"] * 10 + ["plan_limit_reached"]
    [after] = _subs(user["id"])
    assert after["ops_used"] == 10
    [count] = _sql("SELECT COUNT(*) AS n FROM generation_jobs WHERE user_id = %s", (user["id"],), fetch=True)
    assert count["n"] == 10


def test_concurrent_requests_never_exceed_ops_limit():
    """trailer_start = 8 operations, 16 racing requests -> exactly 8 jobs."""
    from scripts import prod_job_store
    user, sub = _active_sub("trailer_start")

    def attempt(_i):
        return prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], str(sub["id"]),
                                                                    mode="trailer_film_breaker"))["outcome"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(16)))
    assert outcomes.count("created") == 8
    assert outcomes.count("plan_limit_reached") == 8
    [after] = _subs(user["id"])
    assert after["ops_used"] == 8


def test_idempotent_replay_is_not_charged_twice():
    from scripts import prod_job_store
    user, sub = _active_sub()
    kw = _job_kwargs(user["id"], str(sub["id"]), idempotency_key="same-click", idempotency_fingerprint="fp")
    first = prod_job_store.create_job_with_quota(**kw)
    replay = prod_job_store.create_job_with_quota(**{**kw, "job_id": str(uuid.uuid4())})
    assert first["outcome"] == "created" and replay["outcome"] == "existing"
    assert replay["job"]["id"] == first["job"]["id"]
    [after] = _subs(user["id"])
    assert after["ops_used"] == 1


def test_failed_job_insert_rolls_back_the_debit():
    """Job insert fails (duplicate job id) -> the operation is not spent."""
    from scripts import prod_job_store
    user, sub = _active_sub()
    kw = _job_kwargs(user["id"], str(sub["id"]))
    prod_job_store.create_job_with_quota(**kw)
    with pytest.raises(Exception):
        prod_job_store.create_job_with_quota(**kw)  # same job_id -> PK violation
    [after] = _subs(user["id"])
    assert after["ops_used"] == 1


def test_expired_period_is_not_charged():
    from scripts import prod_job_store
    user, sub = _active_sub()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    _sql("UPDATE user_subscriptions SET period_start = %s, period_end = %s WHERE id = %s",
         (past - timedelta(days=30), past, sub["id"]))
    res = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], str(sub["id"])))
    assert res["outcome"] == "plan_limit_reached"
    [after] = _subs(user["id"])
    assert after["ops_used"] == 0


def test_cannot_charge_another_users_subscription():
    from scripts import prod_job_store
    owner, sub = _active_sub()
    other = _mk_user()
    res = prod_job_store.create_job_with_quota(**_job_kwargs(other["id"], str(sub["id"])))
    assert res["outcome"] == "plan_limit_reached"
    [after] = _subs(owner["id"])
    assert after["ops_used"] == 0


def test_free_plan_debit_unchanged_when_no_subscription():
    from scripts import prod_job_store
    user = _mk_user(free_video_used=0, free_video_limit=1)
    first = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], None))
    second = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], None))
    assert (first["outcome"], second["outcome"]) == ("created", "quota_exceeded")
    assert first["job"]["charged_subscription_id"] is None
