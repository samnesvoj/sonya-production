"""
POST /api/billing/robokassa/result -- ResultURL webhook contract.

payment_store is monkeypatched (module-attribute style, same as
tests/test_payment_checkout.py) so these tests never touch a real
Postgres; scripts/robokassa.py's real MD5 signature functions are used as-is
so the tests exercise the actual formula, not a stand-in.
"""
from __future__ import annotations

import hashlib

import pytest

from scripts import payment_store


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _robokassa_env(monkeypatch):
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "sonyagroup")
    monkeypatch.setenv("ROBOKASSA_PASSWORD_1", "prod-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD_2", "prod-pass-2")
    monkeypatch.setenv("ROBOKASSA_TEST_PASSWORD_1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_TEST_PASSWORD_2", "test-pass-2")


def _payment(invoice_id=42, is_test=False, status="pending"):
    return {
        "id": "pay-1", "invoice_id": invoice_id, "user_id": "user-1",
        "plan_id": "pro_30d", "amount": "500.00", "currency": "RUB",
        "is_test": is_test, "status": status,
    }


def test_result_unknown_invoice_rejected_before_signature_check(client, monkeypatch):
    monkeypatch.setattr(payment_store, "get_payment_by_invoice_id", lambda inv_id: None)
    called = {}
    monkeypatch.setattr(
        payment_store, "process_successful_payment",
        lambda *a, **kw: called.setdefault("hit", True),
    )

    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "500.00", "InvId": "999999", "SignatureValue": "irrelevant"},
    )
    assert resp.status_code == 400
    assert resp.text != "OK999999"
    assert "hit" not in called


def test_result_bad_signature_rejected_and_never_activates(client, monkeypatch):
    monkeypatch.setattr(payment_store, "get_payment_by_invoice_id", lambda inv_id: _payment(inv_id))
    called = {}
    monkeypatch.setattr(
        payment_store, "process_successful_payment",
        lambda *a, **kw: called.setdefault("hit", True),
    )

    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "500.00", "InvId": "42", "SignatureValue": "deadbeef"},
    )
    assert resp.status_code == 400
    assert resp.text != "OK42"
    assert "hit" not in called


def test_result_valid_signature_activates_and_returns_exact_ok(client, monkeypatch):
    monkeypatch.setattr(payment_store, "get_payment_by_invoice_id", lambda inv_id: _payment(inv_id, is_test=False))
    monkeypatch.setattr(
        payment_store, "process_successful_payment",
        lambda invoice_id, out_sum, raw_params: {"result": "activated", "payment": _payment(invoice_id, status="paid")},
    )

    sig = _md5("500.00:42:prod-pass-2")
    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "500.00", "InvId": "42", "SignatureValue": sig},
    )
    assert resp.status_code == 200
    assert resp.text == "OK42"  # exact, no JSON wrapper, no extra whitespace


def test_result_test_mode_payment_uses_test_password(client, monkeypatch):
    monkeypatch.setattr(payment_store, "get_payment_by_invoice_id", lambda inv_id: _payment(inv_id, is_test=True))
    monkeypatch.setattr(
        payment_store, "process_successful_payment",
        lambda invoice_id, out_sum, raw_params: {"result": "activated", "payment": _payment(invoice_id, is_test=True, status="paid")},
    )

    # Signed with the PROD password -- must fail for an is_test=True payment.
    prod_sig = _md5("500.00:42:prod-pass-2")
    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "500.00", "InvId": "42", "SignatureValue": prod_sig},
    )
    assert resp.status_code == 400

    test_sig = _md5("500.00:42:test-pass-2")
    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "500.00", "InvId": "42", "SignatureValue": test_sig},
    )
    assert resp.status_code == 200
    assert resp.text == "OK42"


def test_result_repeated_callback_on_already_paid_is_idempotent_ok(client, monkeypatch):
    """already_processed must still answer OK{InvId} (so Robokassa stops
    retrying) but the route never re-runs any activation logic itself --
    that guarantee lives in payment_store.process_successful_payment,
    covered for real concurrency in test_payment_atomicity_postgres.py."""
    monkeypatch.setattr(payment_store, "get_payment_by_invoice_id", lambda inv_id: _payment(inv_id, status="paid"))
    monkeypatch.setattr(
        payment_store, "process_successful_payment",
        lambda invoice_id, out_sum, raw_params: {"result": "already_processed", "payment": _payment(invoice_id, status="paid")},
    )

    sig = _md5("500.00:42:prod-pass-2")
    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "500.00", "InvId": "42", "SignatureValue": sig},
    )
    assert resp.status_code == 200
    assert resp.text == "OK42"


def test_result_amount_mismatch_does_not_activate(client, monkeypatch):
    """Signature valid (so it really is Robokassa calling), but the
    OutSum it sent doesn't match what this payment was created for --
    must not activate, must not return OK."""
    monkeypatch.setattr(payment_store, "get_payment_by_invoice_id", lambda inv_id: _payment(inv_id))
    monkeypatch.setattr(
        payment_store, "process_successful_payment",
        lambda invoice_id, out_sum, raw_params: {"result": "amount_mismatch", "payment": _payment(invoice_id)},
    )

    sig = _md5("1.00:42:prod-pass-2")
    resp = client.post(
        "/api/billing/robokassa/result",
        data={"OutSum": "1.00", "InvId": "42", "SignatureValue": sig},
    )
    assert resp.status_code == 400
    assert resp.text != "OK42"
