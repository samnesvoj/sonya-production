"""
telegram_routes.py
===================
@sonya_group_bot account-linking endpoints for SONYA.

  POST /api/telegram/link-token   (authenticated) — issue a one-time deep link
  POST /api/telegram/webhook      (Telegram secret_token, not session auth)
  POST /api/telegram/unlink       (authenticated)
  GET  /api/telegram/status       (authenticated)

This is account-linking ONLY. No streamer-completion notification is sent
anywhere in this file — see scripts/telegram_bot.py's module docstring and
the batch-foundation audit for why that is a deliberately separate, later
step (it depends on streamer_batches, migration 012, reaching a terminal
state, which nothing here triggers).

Identity for the three authenticated endpoints is resolved exclusively
from the sonya_session cookie (scripts.security.get_current_user) — same
rule as auth_routes.py. The webhook is deliberately NOT session-
authenticated (Telegram's servers have no SONYA session); it is verified
instead by scripts.security.verify_telegram_webhook_secret, checked
before any DB access or update parsing.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from scripts import telegram_bot, telegram_store
from scripts.security import (
    get_current_user,
    new_trace_id,
    verify_browser_origin,
    verify_telegram_webhook_secret,
)
from scripts.security_audit import audit

logger = logging.getLogger(__name__)

router = APIRouter()

# The one and only official SONYA bot — never point this at a different
# bot, and never let it be overridden by client input.
BOT_USERNAME = "sonya_group_bot"

_MSG_SUCCESS = (
    "Telegram подключён к SONYA.\n"
    "Будем писать сюда, когда обработка стрима будет готова."
)
_MSG_EXPIRED = "Ссылка для подключения устарела. Вернитесь в SONYA и запросите новую."
_MSG_USED = "Эта ссылка уже использована. Если нужно подключить Telegram заново, запросите новую ссылку в SONYA."
_MSG_INVALID = "Не удалось распознать ссылку. Попробуйте подключить Telegram из SONYA ещё раз."
_MSG_NO_TOKEN = "Чтобы подключить Telegram, нажмите «Подключить Telegram» в SONYA — оттуда придёт правильная ссылка."
_MSG_SONYA_ALREADY_LINKED = (
    "К этому аккаунту SONYA уже подключён другой Telegram. "
    "Сначала отключите его в SONYA, затем подключите этот заново."
)
_MSG_TELEGRAM_ALREADY_LINKED = (
    "Этот Telegram уже подключён к другому аккаунту SONYA. "
    "Сначала отключите его там, затем подключите заново."
)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


# ── POST /api/telegram/link-token ————————————————————————————————————————————

@router.post("/api/telegram/link-token")
async def create_link_token(
    user: dict = Depends(get_current_user),
    _origin: None = Depends(verify_browser_origin),
):
    trace_id = new_trace_id()
    result = telegram_store.create_link_token(str(user["id"]))
    # The raw token appears in this response body and nowhere else —
    # never logged, never included in the audit event below.
    audit("telegram_link_token_created", user_id=str(user["id"]), trace_id=trace_id)
    return {
        "bot_username": BOT_USERNAME,
        "deep_link": f"https://t.me/{BOT_USERNAME}?start={result['token']}",
        "expires_at": _iso(result["expires_at"]),
    }


# ── POST /api/telegram/webhook ————————————————————————————————————————————————

def _safe_telegram_id(value: Any) -> Optional[int]:
    """
    Validates a Telegram user/chat id WITHOUT a bare int(...) over an
    arbitrary signed value — a malformed (but secret-verified) payload
    must degrade to "ignore this update" here, never to an unhandled
    ValueError/TypeError bubbling up as a 500. Accepts a real int or a
    string of digits (optionally negative — Telegram chat ids for groups
    are negative); anything else returns None.
    """
    if isinstance(value, bool):  # bool is an int subclass in Python — exclude explicitly
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value[1:] if value.startswith("-") else value
        if candidate.isdigit():
            return int(value)
    return None


def _process_start_command(body: dict, trace_id: str) -> None:
    """
    All /start handling for one webhook update. Every REJECTION path here
    (bad token, either direction of already-linked) is a definitive,
    non-retryable outcome — this function returns normally in every one
    of those cases, and the caller (telegram_webhook below) marks the
    update processed regardless. The ONLY way this function signals "this
    needs a real retry" is by letting an exception escape — which happens
    only for a genuinely unexpected/transient failure (e.g. a DB error
    inside telegram_store.link_via_token), never for a malformed payload
    or a business-rule rejection.
    """
    message = body.get("message")
    if not isinstance(message, dict):
        return  # not a message update (could be any other update type) — ignore

    raw_text = message.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}

    chat_id = _safe_telegram_id(chat.get("id"))
    telegram_user_id = _safe_telegram_id(sender.get("id"))

    if not text.startswith("/start") or chat_id is None or telegram_user_id is None:
        # Not a /start we care about, or one of the ids didn't survive
        # validation — ignore silently rather than guess.
        return

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        telegram_bot.send_message(chat_id, _MSG_NO_TOKEN)
        return

    raw_token = parts[1].strip()

    try:
        # Atomic: token consumption and account linking happen in ONE DB
        # transaction (see telegram_store.link_via_token's own docstring)
        # — a transient failure here can never leave "token used, account
        # not linked", and this call is exactly the boundary past which a
        # raised exception means "retry me", not "reject me".
        linked_user = telegram_store.link_via_token(raw_token, telegram_user_id, chat_id)
        user_id = str(linked_user["id"])
    except telegram_store.TelegramLinkTokenExpiredError:
        telegram_bot.send_message(chat_id, _MSG_EXPIRED)
        return
    except telegram_store.TelegramLinkTokenUsedError:
        telegram_bot.send_message(chat_id, _MSG_USED)
        return
    except telegram_store.TelegramLinkTokenInvalidError:
        telegram_bot.send_message(chat_id, _MSG_INVALID)
        return
    except telegram_store.TelegramAccountAlreadyLinkedError:
        telegram_bot.send_message(chat_id, _MSG_SONYA_ALREADY_LINKED)
        return
    except telegram_store.TelegramAlreadyLinkedToAnotherUserError:
        telegram_bot.send_message(chat_id, _MSG_TELEGRAM_ALREADY_LINKED)
        return

    telegram_bot.send_message(chat_id, _MSG_SUCCESS)
    audit("telegram_linked", user_id=user_id, trace_id=trace_id,
          details={"telegram_user_id": telegram_user_id})
    logger.info("[telegram] linked user_id=%s trace_id=%s", user_id, trace_id)


@router.post("/api/telegram/webhook")
async def telegram_webhook(
    request: Request,
    _secret: None = Depends(verify_telegram_webhook_secret),
):
    """
    Returns 200 {"ok": true} for every DEFINITIVE outcome — success, or
    any "expected" rejection (expired/used/invalid token, already-linked
    conflicts) — so Telegram never retries a message that was already
    handled and replied to. A malformed payload (not valid JSON, no
    update_id) also gets a 4xx-but-Telegram-may-retry response, which is
    fine since that shape would never come from a real update.

    A genuinely unexpected exception from _process_start_command() is
    deliberately NOT caught here: it propagates to FastAPI's default 5xx,
    Telegram's own delivery retry redelivers the same update_id later, and
    claim_update()'s lease (see telegram_store.py) allows that retry to
    actually re-run the handler once the lease expires — this is the
    mechanism, not a queue or a framework, that makes "retry after a
    transient failure" work.
    """
    trace_id = new_trace_id()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_payload", "trace_id": trace_id})

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": "invalid_payload", "trace_id": trace_id})

    raw_update_id = body.get("update_id")
    if raw_update_id is None:
        raise HTTPException(status_code=400, detail={"error": "missing_update_id", "trace_id": trace_id})
    try:
        update_id = int(raw_update_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"error": "invalid_update_id", "trace_id": trace_id})

    claim = telegram_store.claim_update(update_id)
    if claim == "skip":
        logger.info("[telegram] update_skipped update_id=%s trace_id=%s", update_id, trace_id)
        return {"ok": True}

    _process_start_command(body, trace_id)

    telegram_store.mark_update_processed(update_id)
    return {"ok": True}


# ── POST /api/telegram/unlink ————————————————————————————————————————————————

@router.post("/api/telegram/unlink")
async def unlink(
    user: dict = Depends(get_current_user),
    _origin: None = Depends(verify_browser_origin),
):
    trace_id = new_trace_id()
    telegram_store.unlink_telegram_account(str(user["id"]))
    audit("telegram_unlinked", user_id=str(user["id"]), trace_id=trace_id)
    return {"ok": True}


# ── GET /api/telegram/status ————————————————————————————————————————————————

@router.get("/api/telegram/status")
async def telegram_status(user: dict = Depends(get_current_user)):
    # Deliberately excludes telegram_user_id / telegram_chat_id — the
    # frontend never needs the raw Telegram identity, only whether it's
    # linked and since when.
    return {
        "linked": bool(user.get("telegram_linked", False)),
        "bot_username": BOT_USERNAME,
        "linked_at": _iso(user.get("telegram_linked_at")),
    }
