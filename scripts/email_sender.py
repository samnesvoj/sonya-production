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

logger = logging.getLogger(__name__)


class EmailNotConfiguredError(RuntimeError):
    """Raised when SMTP env vars are missing/incomplete."""


class EmailSendError(RuntimeError):
    """Raised when SMTP is configured but sending fails (connection, auth, etc.)."""


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


def _render_code_email(code: str, purpose: str) -> tuple[str, str]:
    """Returns (subject, plain_text_body). Never called with logging enabled."""
    if purpose == "register":
        subject = "Your SONYA verification code"
        intro = "Confirm your email to finish creating your SONYA account."
    else:
        subject = "Your SONYA sign-in code"
        intro = "Use this code to sign in to SONYA."

    body = (
        f"{intro}\n\n"
        f"    {code}\n\n"
        f"This code expires in 10 minutes and can only be used once.\n"
        f"If you did not request this, you can safely ignore this email."
    )
    return subject, body


def send_auth_code_email(to_email: str, code: str, purpose: str) -> None:
    """
    Send a one-time auth code by email.

    Raises:
        EmailNotConfiguredError: SMTP env vars missing — caller returns 503.
        EmailSendError: SMTP configured but send failed — caller returns 502.
    """
    cfg = _smtp_config()
    subject, body = _render_code_email(code, purpose)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    msg["To"] = to_email
    msg.set_content(body)

    try:
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
    except EmailNotConfiguredError:
        raise
    except Exception as exc:
        # Never include the code in the error text.
        logger.error("[email] send_failed purpose=%s host=%s error=%s", purpose, cfg["host"], exc)
        raise EmailSendError("failed to send auth code email") from exc

    logger.info("[email] auth_code_sent purpose=%s host=%s", purpose, cfg["host"])
