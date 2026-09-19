"""
Pure signature-logic tests for scripts/robokassa.py -- no DB, no FastAPI.
Verified formulas (see scripts/robokassa.py docstring / the payment plan
notes): MerchantLogin:OutSum:InvId[:Receipt]:Password#1 for the outgoing
payment signature, OutSum:InvId:Password#2 for ResultURL.
"""
from __future__ import annotations

import hashlib

import pytest

from scripts import robokassa


@pytest.fixture(autouse=True)
def _robokassa_env(monkeypatch):
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "sonyagroup")
    monkeypatch.setenv("ROBOKASSA_PASSWORD_1", "prod-pass-1")
    monkeypatch.setenv("ROBOKASSA_PASSWORD_2", "prod-pass-2")
    monkeypatch.setenv("ROBOKASSA_TEST_PASSWORD_1", "test-pass-1")
    monkeypatch.setenv("ROBOKASSA_TEST_PASSWORD_2", "test-pass-2")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ── format_out_sum ────────────────────────────────────────────────────────

def test_format_out_sum_plain_decimal():
    from decimal import Decimal
    assert robokassa.format_out_sum(Decimal("500.00")) == "500.00"


# ── build_payment_signature ───────────────────────────────────────────────

def test_build_payment_signature_no_receipt():
    sig = robokassa.build_payment_signature("500.00", 42, None, "prod-pass-1")
    assert sig == _md5("sonyagroup:500.00:42:prod-pass-1")


def test_build_payment_signature_with_receipt_includes_segment():
    receipt = '{"items":[{"name":"x"}]}'
    sig = robokassa.build_payment_signature("500.00", 42, receipt, "prod-pass-1")
    assert sig == _md5(f"sonyagroup:500.00:42:{receipt}:prod-pass-1")
    # No Receipt -> no placeholder slot, different signature entirely.
    assert sig != robokassa.build_payment_signature("500.00", 42, None, "prod-pass-1")


def test_build_payment_url_uses_test_password_and_flag_when_is_test():
    url = robokassa.build_payment_url(
        out_sum="500.00", inv_id=1, description="SONYA Pro", receipt_json=None, is_test=True,
    )
    assert "IsTest=1" in url
    expected_sig = _md5("sonyagroup:500.00:1:test-pass-1")
    assert expected_sig in url


def test_build_payment_url_prod_has_no_is_test_flag():
    url = robokassa.build_payment_url(
        out_sum="500.00", inv_id=1, description="SONYA Pro", receipt_json=None, is_test=False,
    )
    assert "IsTest" not in url
    expected_sig = _md5("sonyagroup:500.00:1:prod-pass-1")
    assert expected_sig in url


# ── verify_result_signature ───────────────────────────────────────────────

def test_verify_result_signature_valid_prod():
    sig = _md5("500.00:42:prod-pass-2")
    assert robokassa.verify_result_signature("500.00", 42, sig, is_test=False) is True


def test_verify_result_signature_valid_test():
    sig = _md5("500.00:42:test-pass-2")
    assert robokassa.verify_result_signature("500.00", 42, sig, is_test=True) is True


def test_verify_result_signature_wrong_password_mode_fails():
    # Signed with the test password, but checked as if this were a
    # production payment -- must fail (never mix test/prod passwords).
    sig = _md5("500.00:42:test-pass-2")
    assert robokassa.verify_result_signature("500.00", 42, sig, is_test=False) is False


def test_verify_result_signature_tampered_fails():
    sig = _md5("500.00:42:prod-pass-2")
    assert robokassa.verify_result_signature("999.00", 42, sig, is_test=False) is False


def test_verify_result_signature_uses_raw_out_sum_not_reformatted():
    """Production Robokassa may echo more decimal places than test mode
    (e.g. "500.000000" vs "500.00") for the same amount -- the signature
    must be checked against exactly what was received, never a
    re-formatted copy of it."""
    raw = "500.000000"
    sig = _md5(f"{raw}:42:prod-pass-2")
    assert robokassa.verify_result_signature(raw, 42, sig, is_test=False) is True
    # A signature computed over the "clean" 500.00 form does NOT validate
    # against the raw 500.000000 string, and vice versa -- this is exactly
    # why the caller must never normalize OutSum before verifying.
    other_sig = _md5("500.00:42:prod-pass-2")
    assert robokassa.verify_result_signature(raw, 42, other_sig, is_test=False) is False


def test_verify_result_signature_case_insensitive_hex():
    sig = _md5("500.00:42:prod-pass-2").upper()
    assert robokassa.verify_result_signature("500.00", 42, sig, is_test=False) is True


# ── build_receipt ─────────────────────────────────────────────────────────

def test_build_receipt_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ROBOKASSA_RECEIPT_ENABLED", raising=False)
    plan = robokassa.PLAN_CATALOG["pro_30d"]
    assert robokassa.build_receipt(plan) is None


def test_build_receipt_enabled_requires_tax(monkeypatch):
    monkeypatch.setenv("ROBOKASSA_RECEIPT_ENABLED", "true")
    monkeypatch.delenv("ROBOKASSA_RECEIPT_TAX", raising=False)
    plan = robokassa.PLAN_CATALOG["pro_30d"]
    with pytest.raises(RuntimeError):
        robokassa.build_receipt(plan)


def test_build_receipt_enabled_builds_json_matching_out_sum(monkeypatch):
    monkeypatch.setenv("ROBOKASSA_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("ROBOKASSA_RECEIPT_TAX", "vat0")
    plan = robokassa.PLAN_CATALOG["pro_30d"]
    receipt_json = robokassa.build_receipt(plan)
    assert receipt_json is not None
    import json
    receipt = json.loads(receipt_json)
    assert receipt["items"][0]["tax"] == "vat0"
    assert receipt["items"][0]["sum"] == float(plan.amount)
    assert "sno" not in receipt  # not set -> omitted, falls back to cabinet default
