"""
Payment-provider layer (scripts/payment_providers.py) and the provider
independent checkout -- no Robokassa involved unless a test enables it.

The flow these protect:
    plan_id -> pricing catalog -> payments row (provider recorded)
            -> provider confirms -> exact plan_id activated
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from scripts import auth_store, payment_providers, payment_store
from scripts.auth_security import hash_session_token
from tests.conftest import make_session, make_user


def _login(client, monkeypatch):
    user = make_user()
    token = f"session-{uuid.uuid4()}"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)
    client.cookies.set("sonya_session", token)
    return user


# ── Registry ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "  ", "selfemployed", "unknown-provider"])
def test_no_provider_unless_one_is_implemented_and_selected(monkeypatch, value):
    monkeypatch.setenv("PAYMENT_PROVIDER", value)
    assert payment_providers.get_checkout_provider() is None


def test_unset_provider_means_none(monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    assert payment_providers.get_checkout_provider() is None


def test_robokassa_is_opt_in_legacy_only(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDER", "Robokassa")
    provider = payment_providers.get_checkout_provider()
    assert provider is not None and provider.name == "robokassa"


# ── Checkout without a provider ──────────────────────────────────────────

def test_checkout_without_provider_is_503_and_creates_no_payment(client, monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    _login(client, monkeypatch)
    monkeypatch.setattr(payment_store, "create_pending_payment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no payment row without a provider")))
    resp = client.post("/api/billing/checkout", json={"plan_id": "cut_pro"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "payment_unavailable"


def test_unknown_plan_rejected_before_provider_is_consulted(client, monkeypatch):
    _login(client, monkeypatch)
    monkeypatch.setattr("scripts.payment_routes.get_checkout_provider",
                        lambda: (_ for _ in ()).throw(AssertionError("provider must not be reached")))
    resp = client.post("/api/billing/checkout", json={"plan_id": "pro_30d"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_plan"


# ── Checkout with an arbitrary provider ──────────────────────────────────

class _RecordingProvider:
    name = "recording"

    def __init__(self):
        self.seen = None

    def is_test_mode(self):
        return False

    def create_checkout(self, payment, plan):
        self.seen = (payment, plan)
        return "https://pay.example/redirect"


def test_checkout_hands_provider_the_server_plan_and_records_provider(client, monkeypatch):
    _login(client, monkeypatch)
    provider = _RecordingProvider()
    monkeypatch.setattr("scripts.payment_routes.get_checkout_provider", lambda: provider)
    created = {}

    def fake_create_pending_payment(**kw):
        created.update(kw)
        return {"id": "p1", "invoice_id": 501, **kw, "status": "pending"}
    monkeypatch.setattr(payment_store, "create_pending_payment", fake_create_pending_payment)

    resp = client.post("/api/billing/checkout", json={"plan_id": "trailer_studio", "amount": "1.00"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"invoice_id": 501, "redirect_url": "https://pay.example/redirect"}
    assert created["provider"] == "recording"
    assert created["amount"] == Decimal("4990.00")
    payment, plan = provider.seen
    assert plan.plan_id == "trailer_studio" and payment["plan_id"] == "trailer_studio"


def test_provider_misconfiguration_fails_safely(client, monkeypatch):
    _login(client, monkeypatch)

    class _Broken(_RecordingProvider):
        def create_checkout(self, payment, plan):
            raise RuntimeError("SECRET_ENV_NAME missing")
    monkeypatch.setattr("scripts.payment_routes.get_checkout_provider", lambda: _Broken())
    monkeypatch.setattr(payment_store, "create_pending_payment",
                        lambda **kw: {"id": "p1", "invoice_id": 502, **kw, "status": "pending"})
    resp = client.post("/api/billing/checkout", json={"plan_id": "cut_start"})
    assert resp.status_code == 500
    assert "SECRET_ENV_NAME" not in resp.text


# ── Checkout availability (drives the pay CTA) ───────────────────────────

def test_checkout_availability_is_false_without_provider_and_public(client, monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    resp = client.get("/api/billing/checkout-availability")  # no session: guests see plans too
    assert resp.status_code == 200
    assert resp.json() == {"available": False}


def test_checkout_availability_turns_on_with_a_provider(client, monkeypatch):
    monkeypatch.setattr("scripts.payment_routes.get_checkout_provider", lambda: _RecordingProvider())
    assert client.get("/api/billing/checkout-availability").json() == {"available": True}
