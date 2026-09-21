"""
telegram_bot.py
================
Minimal Telegram Bot API client for @sonya_group_bot (the official SONYA
bot — never create or point at a different bot here).

TELEGRAM_BOT_TOKEN is read from env only: never hardcoded, never returned
to the frontend, never logged. The token is only ever interpolated into
the request URL passed to `requests.post` — never into a log format
string or an f-string that later gets logged. Exceptions from `requests`
often embed the request URL in their own str() (e.g. ConnectionError) —
so on failure this module logs `type(exc).__name__` and an HTTP status
code only, never `str(exc)` and never the URL itself.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_REQUEST_TIMEOUT_SEC = 10


def _get_bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    return token


def send_message(
    chat_id: int,
    text: str,
    button_text: Optional[str] = None,
    button_url: Optional[str] = None,
) -> bool:
    """
    Best-effort send. Returns True on a confirmed 2xx from Telegram, False
    otherwise — never raises past this function. The webhook handler calls
    this only after the account is already linked (or a rejection reason
    already decided), so a failed reply must never undo or block the
    underlying linking decision — it only means the user doesn't see the
    confirmation text in Telegram.

    button_text/button_url (both required together, otherwise ignored):
    adds a single inline URL button below the message — used by the
    streamer completion notification (scripts/streamer_notify.py) to link
    straight to the finished batch. Deliberately just one optional
    parameter pair, not a general reply_markup passthrough — this stays
    the one Telegram client for the whole codebase (see module docstring)
    without becoming a full Bot API wrapper.
    """
    token = _get_bot_token()
    url = f"{_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if button_text and button_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": button_text, "url": button_url}]]}
    try:
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT_SEC)
        if not resp.ok:
            logger.warning("[telegram] send_message_failed chat_id=%s status=%s", chat_id, resp.status_code)
            return False
        return True
    except requests.RequestException as exc:
        # type(exc).__name__ only — str(exc) can embed the request URL,
        # which contains the bot token.
        logger.warning("[telegram] send_message_error chat_id=%s error_type=%s", chat_id, type(exc).__name__)
        return False
