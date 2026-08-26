"""
Tests for scripts/email_sender.py — SMTP transport + template wiring.

SMTP itself is monkeypatched (smtplib.SMTP/SMTP_SSL); no network is used.
"""
from __future__ import annotations

import pytest

from scripts import email_sender
from scripts.auth_security import AUTH_CODE_TTL_SECONDS


class _FakeSMTP:
    sent_messages: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def has_extn(self, name):
        return False

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent_messages.append(msg)


@pytest.fixture()
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pass")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "auth@sonya.group")
    monkeypatch.setenv("SMTP_FROM_NAME", "SONYA")
    monkeypatch.delenv("EMAIL_LOGO_URL", raising=False)


def test_send_auth_code_email_builds_multipart_message(smtp_env, monkeypatch):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)

    email_sender.send_auth_code_email("user@example.com", "123456", "login")

    assert len(_FakeSMTP.sent_messages) == 1
    msg = _FakeSMTP.sent_messages[0]
    assert msg.is_multipart()

    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    assert text_part is not None
    assert html_part is not None
    assert "123456" in text_part.get_content()
    assert "123456" in html_part.get_content()


def test_send_auth_code_email_expiry_matches_config(smtp_env, monkeypatch):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)

    email_sender.send_auth_code_email("user@example.com", "123456", "login")

    msg = _FakeSMTP.sent_messages[0]
    html_part = msg.get_body(preferencelist=("html",))
    expected_minutes = str(AUTH_CODE_TTL_SECONDS // 60)
    assert expected_minutes in html_part.get_content()


def test_send_auth_code_email_not_configured_raises(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM_EMAIL", raising=False)
    with pytest.raises(email_sender.EmailNotConfiguredError):
        email_sender.send_auth_code_email("user@example.com", "123456", "login")


def test_send_auth_code_email_never_logs_raw_code(smtp_env, monkeypatch, caplog):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)

    with caplog.at_level("INFO"):
        email_sender.send_auth_code_email("user@example.com", "987654", "login")

    assert "987654" not in caplog.text


def test_send_auth_code_email_embeds_banner_when_asset_present(smtp_env, monkeypatch):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)

    email_sender.send_auth_code_email("user@example.com", "123456", "login")

    msg = _FakeSMTP.sent_messages[0]
    html_part = msg.get_body(preferencelist=("html",))
    assert f"cid:{email_sender.LOGIN_BANNER_CID}" in html_part.get_content()

    inline_images = [
        part for part in msg.walk()
        if part.get_content_type().startswith("image/")
    ]
    assert len(inline_images) == 1
    assert inline_images[0]["Content-ID"] == f"<{email_sender.LOGIN_BANNER_CID}>"
    # The code must still be fully present and visible alongside the banner.
    assert "123456" in html_part.get_content()


def test_send_auth_code_email_falls_back_to_wordmark_when_banner_asset_missing(smtp_env, monkeypatch):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_sender, "_LOGIN_BANNER_PATH", email_sender._LOGIN_BANNER_PATH.parent / "does-not-exist.jpg")

    # Must not raise — the code email is critical and must still send.
    email_sender.send_auth_code_email("user@example.com", "123456", "login")

    msg = _FakeSMTP.sent_messages[0]
    html_part = msg.get_body(preferencelist=("html",))
    assert "cid:" not in html_part.get_content()
    assert "123456" in html_part.get_content()
    inline_images = [part for part in msg.walk() if part.get_content_type().startswith("image/")]
    assert inline_images == []


# ── welcome email ————————————————————————————————————————————————————————————

def test_send_welcome_email_embeds_banner_as_inline_cid_image(smtp_env, monkeypatch):
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)

    email_sender.send_welcome_email("newuser@example.com")

    assert len(_FakeSMTP.sent_messages) == 1
    msg = _FakeSMTP.sent_messages[0]

    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    assert text_part is not None
    assert html_part is not None
    assert "Добро пожаловать в SONYA" in html_part.get_content()

    inline_images = [
        part for part in msg.walk()
        if part.get_content_type().startswith("image/")
    ]
    assert len(inline_images) == 1
    image_part = inline_images[0]
    assert image_part.get_content_disposition() in ("inline", None)
    assert image_part["Content-ID"] == f"<{email_sender.WELCOME_BANNER_CID}>"
    assert len(image_part.get_payload(decode=True)) > 0


def test_send_welcome_email_not_configured_raises(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM_EMAIL", raising=False)
    with pytest.raises(email_sender.EmailNotConfiguredError):
        email_sender.send_welcome_email("newuser@example.com")


def test_send_welcome_email_missing_asset_raises(smtp_env, monkeypatch):
    monkeypatch.setattr(email_sender, "_WELCOME_BANNER_PATH", email_sender._WELCOME_BANNER_PATH.parent / "does-not-exist.jpg")
    with pytest.raises(email_sender.EmailAssetMissingError):
        email_sender.send_welcome_email("newuser@example.com")


def test_login_and_welcome_emails_use_different_banner_assets(smtp_env, monkeypatch):
    """Distinct images: welcome banner has 'ДОБРО ПОЖАЛОВАТЬ' baked in, the
    login banner is the plain wordmark — sending each must attach its own file."""
    _FakeSMTP.sent_messages = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)

    email_sender.send_auth_code_email("user@example.com", "123456", "login")
    email_sender.send_welcome_email("user@example.com")

    login_msg, welcome_msg = _FakeSMTP.sent_messages
    login_image = next(p for p in login_msg.walk() if p.get_content_type().startswith("image/"))
    welcome_image = next(p for p in welcome_msg.walk() if p.get_content_type().startswith("image/"))

    assert login_image["Content-ID"] == f"<{email_sender.LOGIN_BANNER_CID}>"
    assert welcome_image["Content-ID"] == f"<{email_sender.WELCOME_BANNER_CID}>"
    assert login_image.get_payload(decode=True) != welcome_image.get_payload(decode=True)
