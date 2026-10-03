"""
telegram_security.py
=====================
Cryptographic helpers for Telegram account linking (@sonya_group_bot).

Mirrors auth_security.py's session-token pattern exactly: a
cryptographically random one-time link token is the ONLY thing ever sent
to the browser / embedded in the t.me deep link; only its HMAC-SHA256
hash is ever written to Postgres (see scripts/telegram_store.py). Keyed by
the existing AUTH_SECRET -- this is the same security domain as session
tokens and auth codes (a short-lived, single-use credential proving "this
browser request is who it claims to be"), so no new secret is invented
just for this. TELEGRAM_BOT_TOKEN / TELEGRAM_WEBHOOK_SECRET (see
telegram_bot.py / scripts/security.py) are a different domain entirely
(they authenticate Telegram's own servers, not a SONYA user) and stay
separate.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

# Short-lived by design (one-time deep link, meant to be used within the
# same browser session that requested it) -- overridable per deployment,
# single canonical default defined once here.
LINK_TOKEN_TTL_SECONDS = int(os.environ.get("TELEGRAM_LINK_TOKEN_TTL_SECONDS", str(15 * 60)))  # 15 min


def _get_auth_secret() -> str:
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        raise RuntimeError(
            "AUTH_SECRET not configured. Set AUTH_SECRET to a long random value "
            "(e.g. openssl rand -hex 32) — required to hash Telegram link tokens."
        )
    return secret


def generate_link_token() -> str:
    """Opaque random token — the only value ever sent to the browser /
    embedded in the t.me deep link. Never logged, never persisted raw."""
    return secrets.token_urlsafe(32)


def hash_link_token(token: str) -> str:
    """HMAC-SHA256(AUTH_SECRET, token) — the only value ever stored in Postgres."""
    secret = _get_auth_secret().encode("utf-8")
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


def link_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=LINK_TOKEN_TTL_SECONDS)
