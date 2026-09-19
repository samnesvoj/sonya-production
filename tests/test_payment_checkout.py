"""
POST /api/billing/checkout -- price/plan integrity and route-path tests.

Uses the same cookie-session pattern as tests/test_generation_jobs_auth.py:
a real session cookie on the TestClient, with scripts.auth_store's DB calls
monkeypatched (no real Postgres).
"""
from __future__ import annotations

import pytest

from scripts import auth_store, payment_store
from scripts.auth_security import hash_session_token
from tests.conftest import make_session, make_user


@pytest.fixture(autouse=True)
def _robokassa_env(monkeypatch):
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "sonyagroup")
    monkeypatch.setenv("ROBOKASSA_PASSWORD_1", "prod-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD_2", "prod-pass-2")
    monkeypatch.setenv("ROBOKASSA_TEST_MODE", "true")
    monkeypatch.setenv("ROBOKASSA_TEST_PASSWORD_1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_TEST_PASSWORD_2", "test-pass-2")


def _login(client, monkeypatch, user=None):
    user = user or make_user()
    token = "raw-session-token"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)
    client.cookies.set("sonya_session", token)
    return user


def test_checkout_requires_session(client):
    resp = client.post("/api/billing/checkout", json={"plan_id": "pro_30d"})
    assert resp.status_code == 401


def test_checkout_unknown_plan_is_400(client, monkeypatch):
    _login(client, monkeypatch)
    resp = client.post("/api/billing/checkout", json={"plan_id": "does_not_exist"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_plan"


def test_checkout_uses_actual_route_path(client, monkeypatch):
    """Exact path assertion -- catches a prefix mistake (e.g. router
    included with an accidental prefix="/api", which would double it, or
    the route missing /api entirely)."""
    _login(client, monkeypatch)
    monkeypatch.setattr(
        payment_store, "create_pending_payment",
        lambda user_id, plan_id, amount, is_test, plan_type, duration_days: {
            "id": "p1", "invoice_id": 12345, "user_id": user_id,
            "plan_id": plan_id, "amount": amount, "is_test": is_test, "status": "pending",
        },
    )
    resp = client.post("/api/billing/checkout", json={"plan_id": "pro_30d"})
    assert resp.status_code == 200, resp.text


def test_checkout_price_comes_from_server_catalog_not_client(client, monkeypatch):
    """The classic 'tamper the price' attack: the client sends an extra
    amount field. It must be silently ignored -- checkout only ever
    forwards plan_id, and the server looks up the real price itself."""
    _login(client, monkeypatch)
    captured = {}

    def fake_create_pending_payment(user_id, plan_id, amount, is_test, plan_type, duration_days):
        captured["amount"] = amount
        captured["plan_type"] = plan_type
        captured["duration_days"] = duration_days
        return {
            "id": "p1", "invoice_id": 999, "user_id": user_id,
            "plan_id": plan_id, "amount": amount, "is_test": is_test, "status": "pending",
        }

    monkeypatch.setattr(payment_store, "create_pending_payment", fake_create_pending_payment)

    resp = client.post("/api/billing/checkout", json={"plan_id": "pro_30d", "amount": "0.01", "price": 1})
    assert resp.status_code == 200, resp.text

    from decimal import Decimal
    from scripts.robokassa import PLAN_CATALOG
    assert captured["amount"] == PLAN_CATALOG["pro_30d"].amount
    assert captured["amount"] != Decimal("0.01")
    assert captured["plan_type"] == PLAN_CATALOG["pro_30d"].plan_type
    assert captured["duration_days"] == PLAN_CATALOG["pro_30d"].duration_days

    body = resp.json()
    assert body["invoice_id"] == 999
    assert "auth.robokassa.ru" in body["redirect_url"]
    assert "OutSum=500.00" in body["redirect_url"]


def test_checkout_missing_credentials_fails_safely(client, monkeypatch):
    _login(client, monkeypatch)
    # ROBOKASSA_TEST_MODE=true (set by the autouse fixture) -- checkout
    # uses the TEST password, so that's the one that must be missing here.
    monkeypatch.delenv("ROBOKASSA_TEST_PASSWORD_1", raising=False)
    monkeypatch.setattr(
        payment_store, "create_pending_payment",
        lambda user_id, plan_id, amount, is_test, plan_type, duration_days: {
            "id": "p1", "invoice_id": 1, "user_id": user_id,
            "plan_id": plan_id, "amount": amount, "is_test": is_test, "status": "pending",
        },
    )
    resp = client.post("/api/billing/checkout", json={"plan_id": "pro_30d"})
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "payment_unavailable"
