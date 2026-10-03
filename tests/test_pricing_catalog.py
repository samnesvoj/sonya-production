"""
Pricing catalog (scripts/pricing.py) -- payment-provider independent.

The server catalog is the only source of price and terms; the frontend's
SONYA_PRICING (auth.js) only displays them, so the two must never drift.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import pricing

PUBLIC_PLANS = {
    # plan_id: (mode, price, ops, max_source_min)
    "cut_start": ("cut", "1090.00", 10, 60),
    "cut_pro": ("cut", "2690.00", 20, 120),
    "cut_studio": ("cut", "4990.00", 30, 180),
    "trailer_start": ("trailer", "1190.00", 8, 90),
    "trailer_pro": ("trailer", "2190.00", 12, 120),
    "trailer_studio": ("trailer", "4990.00", 24, 180),
    "streamer_start": ("streamer", "1990.00", 10, 240),
}


@pytest.mark.parametrize("plan_id", sorted(PUBLIC_PLANS))
def test_each_public_plan_has_its_server_side_terms(plan_id):
    mode, price, ops, max_min = PUBLIC_PLANS[plan_id]
    plan = pricing.get_plan(plan_id)
    assert plan is not None and plan.plan_id == plan_id
    assert plan.amount == Decimal(price)
    assert (plan.plan_mode, plan.ops_limit, plan.max_source_sec) == (mode, ops, max_min * 60)
    assert plan.duration_days == 30
    # Never the legacy unlimited, all-modes entitlement.
    assert plan.plan_type == "subscription"


def test_catalog_has_exactly_the_public_plans():
    assert set(pricing.PLAN_CATALOG) == set(PUBLIC_PLANS)
    for hidden in ("streamer_pro", "streamer_max", "pro_30d", ""):
        assert pricing.get_plan(hidden) is None


def test_catalog_does_not_depend_on_a_payment_provider():
    tree = ast.parse(Path(pricing.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("scripts") for m in imported), imported


def test_frontend_pricing_matches_server_catalog():
    auth_js = (Path(__file__).resolve().parent.parent / "auth.js").read_text(encoding="utf-8")
    block = auth_js[auth_js.index("const SONYA_PRICING"):auth_js.index("const _paywall")]
    modes = re.findall(r"id: '(\w+)', label: '[^']+', quotaLabel: '[^']+',\s+plans: \[(.*?)\]", block, re.S)
    front = {}
    for mode_id, plans in modes:
        for pid, price, quota, source in re.findall(
                r"id: '(\w+)',\s+name: '\w+',\s+price: (\d+),\s*quota: (\d+),\s*source: '([^']+)'", plans):
            front[pid] = (mode_id, price, int(quota), source)
    assert set(front) == set(PUBLIC_PLANS)
    for pid, (mode, price, ops, max_min) in PUBLIC_PLANS.items():
        f_mode, f_price, f_ops, f_source = front[pid]
        assert (f_mode, Decimal(f_price), f_ops) == (mode, Decimal(price), ops), pid
        shown_min = int(re.search(r"\d+", f_source).group()) * (60 if "час" in f_source else 1)
        assert shown_min == max_min, pid
