"""
email_sender.py
================
SMTP email sender abstraction for SONYA auth codes.

Env vars:
  SMTP_HOST
  SMTP_PORT           (default 587)
  SMTP_USERNAME
  SMTP_PASSWORD
  SMTP_FROM_EMAIL
  SMTP_FROM_NAME
  EMAIL_LOGO_URL      (optional, absolute HTTPS URL; not a secret — falls
                       back to a text wordmark when unset, see
                       scripts/email_templates.py)

If SMTP is not fully configured, `send_auth_code_email` raises
EmailNotConfiguredError. Callers MUST turn this into HTTP 503
{"error": "email_not_configured"} — auth must never silently succeed
without actually sending the code.

The raw code is NEVER logged. Only metadata (recipient domain hash-free,
purpose, message id) may be logged at INFO level.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from scripts.auth_security import AUTH_CODE_TTL_SECONDS
from scripts.email_templates import (
    LOGIN_BANNER_CID,
    WELCOME_BANNER_CID,
    render_login_code_email,
    render_welcome_email,
)

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "email"
_LOGIN_BANNER_PATH = _ASSETS_DIR / "login-banner.jpg"
_WELCOME_BANNER_PATH = _ASSETS_DIR / "welcome-banner.jpg"


class EmailNotConfiguredError(RuntimeError):
    """Raised when SMTP env vars are missing/incomplete."""


class EmailSendError(RuntimeError):
    """Raised when SMTP is configured but sending fails (connection, auth, etc.)."""


class EmailAssetMissingError(RuntimeError):
    """Raised when a bundled email asset (e.g. the welcome banner image) is missing on disk."""


def _smtp_config() -> dict:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = os.environ.get("SMTP_PORT", "").strip()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_email = os.environ.get("SMTP_FROM_EMAIL", "").strip()
    from_name = os.environ.get("SMTP_FROM_NAME", "SONYA").strip()

    if not host or not from_email:
        raise EmailNotConfiguredError(
            "SMTP is not configured (SMTP_HOST / SMTP_FROM_EMAIL missing)"
        )

    try:
        port_int = int(port) if port else 587
    except ValueError:
        port_int = 587

    return {
        "host": host,
        "port": port_int,
        "username": username,
        "password": password,
        "from_email": from_email,
        "from_name": from_name or "SONYA",
    }


def is_email_configured() -> bool:
    try:
        _smtp_config()
        return True
    except EmailNotConfiguredError:
        return False


def _read_banner_bytes(path: Path) -> bytes | None:
    """Returns the banner image bytes at `path`, or None if missing on disk."""
    if not path.is_file():
        return None
    return path.read_bytes()


def _attach_banner(msg: EmailMessage, banner_bytes: bytes, cid: str) -> None:
    html_part = msg.get_payload()[1]
    html_part.add_related(
        banner_bytes,
        maintype="image",
        subtype="jpeg",
        cid=f"<{cid}>",
    )


_PURPOSE_SUBJECTS = {
    "register": ("Ваш код подтверждения SONYA", "Ваш код подтверждения регистрации в SONYA"),
    "email_verify": ("Ваш код подтверждения email SONYA", "Подтвердите email в SONYA"),
}


def _render_code_email(code: str, purpose: str, banner_cid: str | None) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body). Never called with logging enabled."""
    subject, heading = _PURPOSE_SUBJECTS.get(purpose, (None, None))
    expires_minutes = max(1, AUTH_CODE_TTL_SECONDS // 60)
    return render_login_code_email(
        code,
        expires_minutes,
        subject=subject,
        heading=heading,
        banner_cid=banner_cid,
    )


def _deliver(cfg: dict, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=15) as smtp:
            if cfg["username"]:
                smtp.login(cfg["username"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as smtp:
            smtp.ehlo()
            if smtp.has_extn("STARTTLS"):
                smtp.starttls(context=context)
                smtp.ehlo()
            if cfg["username"]:
                smtp.login(cfg["username"], cfg["password"])
            smtp.send_message(msg)


def send_auth_code_email(to_email: str, code: str, purpose: str) -> None:
    """
    Send a one-time auth code by email.

    Raises:
        EmailNotConfiguredError: SMTP env vars missing — caller returns 503.
        EmailSendError: SMTP configured but send failed — caller returns 502.
    """
    cfg = _smtp_config()
    banner_bytes = _read_banner_bytes(_LOGIN_BANNER_PATH)
    subject, html_body, text_body = _render_code_email(
        code, purpose, banner_cid=LOGIN_BANNER_CID if banner_bytes else None
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if banner_bytes:
        _attach_banner(msg, banner_bytes, LOGIN_BANNER_CID)

    try:
        _deliver(cfg, msg)
    except EmailNotConfiguredError:
        raise
    except Exception as exc:
        # Never include the code in the error text.
        logger.error("[email] send_failed purpose=%s host=%s error=%s", purpose, cfg["host"], exc)
        raise EmailSendError("failed to send auth code email") from exc

    logger.info("[email] auth_code_sent purpose=%s host=%s", purpose, cfg["host"])


def send_welcome_email(to_email: str) -> None:
    """
    Send the post-registration welcome email, with the SONYA brand banner
    embedded as an inline (CID) image — not a remote-loaded URL.

    This is a non-critical, best-effort send: callers should catch and log
    failures rather than fail the registration flow (see auth_routes.py).

    Raises:
        EmailNotConfiguredError: SMTP env vars missing.
        EmailAssetMissingError: the bundled banner image is missing on disk.
        EmailSendError: SMTP configured but send failed.
    """
    cfg = _smtp_config()

    banner_bytes = _read_banner_bytes(_WELCOME_BANNER_PATH)
    if banner_bytes is None:
        raise EmailAssetMissingError(f"welcome banner asset not found: {_WELCOME_BANNER_PATH}")

    subject, html_body, text_body = render_welcome_email()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    _attach_banner(msg, banner_bytes, WELCOME_BANNER_CID)

    try:
        _deliver(cfg, msg)
    except EmailNotConfiguredError:
        raise
    except Exception as exc:
        logger.error("[email] welcome_send_failed host=%s error=%s", cfg["host"], exc)
        raise EmailSendError("failed to send welcome email") from exc

    logger.info("[email] welcome_sent host=%s", cfg["host"])
