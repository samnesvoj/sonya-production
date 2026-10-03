"""
pricing.py
==========
Server-side source of truth for SONYA's public plans: price, period, and
the entitlement each plan grants. Payment-provider independent -- a
provider (scripts/payment_providers.py) only collects and confirms the
money for a payment whose plan and amount were fixed here at checkout:

    plan_id -> PLAN_CATALOG -> payments row (snapshot) -> provider confirms
            -> payment_store.process_successful_payment() -> user_subscriptions

Ids must match SONYA_PRICING in auth.js. Streamer Pro / Max are
intentionally absent (not public).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Plan:
    plan_id: str
    # Snapshot label stored on payments.plan_type (NOT NULL, migration 010).
    # Public plans use "subscription" -- they never set users.plan_type; their
    # rights live in user_subscriptions (migration 016, entitlements.py).
    plan_type: str
    amount: Decimal
    duration_days: int
    description: str
    # Entitlement terms -- snapshotted onto the payment row at checkout and
    # copied into user_subscriptions on activation.
    plan_mode: str        # "cut" | "trailer" | "streamer"
    ops_limit: int        # operations per period
    max_source_sec: int   # max source video length


def _monthly(plan_id: str, mode: str, amount: str, ops: int, max_source_min: int,
             description: str) -> Plan:
    return Plan(plan_id=plan_id, plan_type="subscription", amount=Decimal(amount),
                duration_days=30, description=description, plan_mode=mode,
                ops_limit=ops, max_source_sec=max_source_min * 60)


PLAN_CATALOG: dict[str, Plan] = {p.plan_id: p for p in (
    _monthly("cut_start",      "cut",      "1090.00", 10,  60, "SONYA Нарезка Start — подписка на 1 месяц"),
    _monthly("cut_pro",        "cut",      "2690.00", 20, 120, "SONYA Нарезка Pro — подписка на 1 месяц"),
    _monthly("cut_studio",     "cut",      "4990.00", 30, 180, "SONYA Нарезка Studio — подписка на 1 месяц"),
    _monthly("trailer_start",  "trailer",  "1190.00",  8,  90, "SONYA Трейлер Start — подписка на 1 месяц"),
    _monthly("trailer_pro",    "trailer",  "2190.00", 12, 120, "SONYA Трейлер Pro — подписка на 1 месяц"),
    _monthly("trailer_studio", "trailer",  "4990.00", 24, 180, "SONYA Трейлер Studio — подписка на 1 месяц"),
    _monthly("streamer_start", "streamer", "1990.00", 10, 240, "SONYA Стример Start — подписка на 1 месяц"),
)}


def get_plan(plan_id: str) -> Optional[Plan]:
    return PLAN_CATALOG.get(plan_id)
