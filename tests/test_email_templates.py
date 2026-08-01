"""
Tests for scripts/email_templates.py — the branded login-code and
post-registration welcome emails.

Pure rendering tests, no SMTP/network involved.
"""
from __future__ import annotations

import pytest

from scripts.email_templates import (
    LOGIN_BANNER_CID,
    WELCOME_BANNER_CID,
    render_login_code_email,
    render_welcome_email,
)


def test_renders_subject_html_and_text():
    subject, html, text = render_login_code_email("123456", 10)
    assert subject
    assert "<html" in html.lower()
    assert text


def test_code_present_in_html_and_text():
    _, html, text = render_login_code_email("482913", 10)
    assert "482913" in html
    assert "482913" in text


def test_code_is_escaped_not_raw_html():
    malicious_code = "<img src=x onerror=alert(1)>"
    _, html, _ = render_login_code_email(malicious_code, 10)
    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img" in html


def test_expiry_minutes_reflected_in_body():
    _, html, text = render_login_code_email("123456", 17)
    assert "17" in html
    assert "17" in text


def test_no_script_or_form_tags():
    _, html, _ = render_login_code_email("123456", 10)
    lower = html.lower()
    assert "<script" not in lower
    assert "<form" not in lower


def test_no_javascript_in_markup():
    _, html, _ = render_login_code_email("123456", 10)
    assert "javascript:" not in html.lower()
    assert "onerror=" not in html.lower()
    assert "onclick=" not in html.lower()


def test_fallback_wordmark_when_no_logo_url(monkeypatch):
    monkeypatch.delenv("EMAIL_LOGO_URL", raising=False)
    _, html, _ = render_login_code_email("123456", 10)
    assert "SONYA" in html
    assert "<img" not in html.lower()


def test_logo_image_used_when_url_provided():
    _, html, _ = render_login_code_email(
        "123456", 10, logo_url="https://sonya.group/logo.png"
    )
    assert "<img" in html.lower()
    assert "https://sonya.group/logo.png" in html
    assert 'alt="SONYA"' in html


def test_logo_url_from_env_var(monkeypatch):
    monkeypatch.setenv("EMAIL_LOGO_URL", "https://sonya.group/env-logo.png")
    _, html, _ = render_login_code_email("123456", 10)
    assert "https://sonya.group/env-logo.png" in html


def test_non_https_env_logo_url_ignored(monkeypatch):
    monkeypatch.setenv("EMAIL_LOGO_URL", "http://insecure.example.com/logo.png")
    _, html, _ = render_login_code_email("123456", 10)
    assert "http://insecure.example.com/logo.png" not in html
    assert "<img" not in html.lower()


def test_logo_has_alt_text():
    _, html, _ = render_login_code_email(
        "123456", 10, logo_url="https://sonya.group/logo.png"
    )
    assert 'alt="SONYA"' in html


def test_ignore_message_present():
    _, html, text = render_login_code_email("123456", 10)
    assert "проигнорируйте" in html
    assert "проигнорируйте" in text


def test_banner_cid_used_as_header_when_provided():
    _, html, _ = render_login_code_email("123456", 10, banner_cid=LOGIN_BANNER_CID)
    assert f'src="cid:{LOGIN_BANNER_CID}"' in html
    assert 'alt="SONYA"' in html


def test_banner_cid_takes_priority_over_logo_url():
    _, html, _ = render_login_code_email(
        "123456", 10, banner_cid=LOGIN_BANNER_CID, logo_url="https://sonya.group/logo.png"
    )
    assert f'src="cid:{LOGIN_BANNER_CID}"' in html
    assert "https://sonya.group/logo.png" not in html


def test_banner_cid_none_falls_back_to_wordmark_or_logo_url():
    _, html, _ = render_login_code_email("123456", 10, banner_cid=None)
    assert "cid:" not in html


def test_all_required_code_content_visible_alongside_banner():
    """The banner must not push out or hide any of the required code-email copy."""
    _, html, text = render_login_code_email("123456", 10, banner_cid=LOGIN_BANNER_CID)
    for fragment in (
        "Ваш код входа в SONYA",
        "123456",
        "Код действителен 10 мин. и может быть использован один раз.",
        "Если вы не запрашивали этот код, просто проигнорируйте письмо.",
        "sonya.group",
        "auth@sonya.group",
    ):
        assert fragment in html, fragment
        assert fragment in text, fragment


def test_site_and_support_links_present():
    _, html, text = render_login_code_email("123456", 10)
    assert "sonya.group" in html
    assert "auth@sonya.group" in html
    assert "sonya.group" in text
    assert "auth@sonya.group" in text


def test_expires_minutes_from_actual_config():
    from scripts.auth_security import AUTH_CODE_TTL_SECONDS

    expires_minutes = AUTH_CODE_TTL_SECONDS // 60
    _, html, _ = render_login_code_email("123456", expires_minutes)
    assert str(expires_minutes) in html


def test_subject_and_heading_overridable_for_other_purposes():
    subject, html, text = render_login_code_email(
        "123456", 10, subject="Custom subject", heading="Custom heading"
    )
    assert subject == "Custom subject"
    assert "Custom heading" in html
    assert "Custom heading" in text


# ── welcome email ————————————————————————————————————————————————————————————

def test_welcome_email_renders_subject_html_and_text():
    subject, html, text = render_welcome_email()
    assert subject
    assert "<html" in html.lower()
    assert text


def test_welcome_email_contains_required_copy():
    _, html, text = render_welcome_email()
    assert "Добро пожаловать в SONYA" in html
    assert "Ваш AI-инструмент для автоматической обработки и монтажа видео" in html
    assert "Добро пожаловать в SONYA" in text
    assert "Ваш AI-инструмент для автоматической обработки и монтажа видео" in text


def test_welcome_email_banner_uses_cid_not_remote_url():
    _, html, _ = render_welcome_email()
    assert f"cid:{WELCOME_BANNER_CID}" in html
    # Never a remote https:// image URL for the banner, and never a data: URI.
    assert "src=\"http" not in html
    assert "src=\"data:" not in html


def test_welcome_email_banner_has_alt_text():
    _, html, _ = render_welcome_email()
    assert f'src="cid:{WELCOME_BANNER_CID}"' in html
    assert 'alt="SONYA' in html


def test_welcome_email_no_script_or_form_tags():
    _, html, _ = render_welcome_email()
    lower = html.lower()
    assert "<script" not in lower
    assert "<form" not in lower


def test_welcome_email_no_javascript_in_markup():
    _, html, _ = render_welcome_email()
    assert "javascript:" not in html.lower()
    assert "onerror=" not in html.lower()
    assert "onclick=" not in html.lower()


def test_welcome_email_uses_table_based_layout():
    _, html, _ = render_welcome_email()
    assert "<table" in html.lower()
    # Not laid out with flexbox/grid as the structural mechanism.
    assert "display:flex" not in html.lower()
    assert "display:grid" not in html.lower()


def test_welcome_email_responsive_meta_and_media_query():
    _, html, _ = render_welcome_email()
    assert 'name="viewport"' in html
    assert "@media" in html


def test_welcome_email_site_and_support_links_present():
    _, html, text = render_welcome_email()
    assert "sonya.group" in html
    assert "auth@sonya.group" in html
    assert "sonya.group" in text
    assert "auth@sonya.group" in text


def test_welcome_email_sora_font_first_in_stack():
    _, html, _ = render_welcome_email()
    assert "font-family:'Sora'" in html or "font-family: 'Sora'" in html


def test_login_and_welcome_banners_use_different_cids():
    """Distinct assets: the welcome banner has 'ДОБРО ПОЖАЛОВАТЬ' baked in, which
    would be misleading on a login-code email seen by returning users."""
    assert LOGIN_BANNER_CID != WELCOME_BANNER_CID
