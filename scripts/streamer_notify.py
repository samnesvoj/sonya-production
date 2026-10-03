"""
scripts/streamer_notify.py
============================
Automatic @sonya_group_bot completion notification for a finished
streamer batch. Reuses the existing scripts/telegram_bot.py client —
no second Telegram client, no new queue/daemon anywhere in this codebase.

TRIGGER (see scripts/prod_generation_api.py's worker/complete + worker/
fail endpoints, and GET /api/streamer/batches/{id} in
scripts/streamer_routes.py): notify_streamer_batch_completion() is called
from a FastAPI BackgroundTasks task, strictly AFTER reconcile_streamer_
batch() has already committed the batch's terminal status — same
"background task after the real state change already landed" pattern
prod_generation_api.py already uses for _cleanup_ephemeral_instance().
This keeps a Telegram failure (or even just Telegram being slow) from
ever blocking or affecting the worker's own /complete response, or making
a browser's GET request wait on Telegram network latency. No new queue
system is introduced — BackgroundTasks is the existing project pattern
for exactly this "fire after commit, don't block the response" shape.

notify_streamer_batch_completion() is deliberately safe to call
UNCONDITIONALLY and REPEATEDLY from anywhere a terminal transition might
have just happened (both trigger points above call it on every request,
not just "the one that actually changed the status") — every early
return inside it is a normal, expected no-op: wrong status, notify
disabled, not linked, already sent, or another process holds the active
claim. See claim_streamer_notification() in scripts/prod_job_store.py for
how at most one process ever gets past the claim for a given batch.

EXACTLY-ONCE LIMITATION (read before touching this file): Telegram's
sendMessage and this module's Postgres commit (mark_streamer_notification_
sent) are two independent systems with no shared transaction. If this
process dies in the narrow window after Telegram has already accepted the
message but before that commit lands, the batch's telegram_notified_at
stays NULL, the claim becomes reclaimable once its lease expires, and a
LATER retry will send a second, duplicate message. This is a real,
accepted possibility — not a bug to "fix" with a more complex pseudo-
transactional scheme, because no such scheme can make an HTTP call to a
third-party API and a local DB commit atomic. SONYA's policy is
deliberately at-least-once: a rare duplicate notification is preferred
over a silently lost one.

RETRY: there is no separate scheduler/daemon. Retry happens opportunistically
— the next time this function is called (another worker /complete or
/fail for a different compose job in the same batch, or a browser's GET
on this batch) after a failed attempt's claim lease
(prod_job_store._NOTIFY_CLAIM_LEASE_SECONDS, currently 300s) has expired.
Honest limitation: if nothing ever calls GET on this batch again and no
other compose job in it completes/fails afterward, a failed notification
may never actually get retried. Acceptable for this pass — the batch's
own GET endpoint is the natural "user still cares about this batch"
signal, and building a standalone retry scheduler for this one case would
be exactly the daemon/queue system this pass was told not to build.
"""
from __future__ import annotations

import logging

from scripts import auth_store, telegram_bot
from scripts.prod_job_store import (
    STREAMER_BATCH_STATUS_PARTIALLY_FAILED,
    STREAMER_BATCH_STATUS_READY,
    claim_streamer_notification,
    get_streamer_batch_by_id,
    mark_streamer_notification_failed,
    mark_streamer_notification_sent,
)

logger = logging.getLogger(__name__)

# Same constant convention as scripts/email_templates.py's own SITE_URL.
SITE_URL = "https://sonya.group"

_MSG_READY = "Клипы готовы ✦\nSONYA закончила обработку стрима."
_MSG_PARTIALLY_FAILED = (
    "Обработка завершена ✦\nБольшинство клипов готово, но часть не удалось собрать."
)
_BUTTON_TEXT = "Открыть клипы"

_ELIGIBLE_STATUSES = (STREAMER_BATCH_STATUS_READY, STREAMER_BATCH_STATUS_PARTIALLY_FAILED)


def notify_streamer_batch_completion(batch_id: str) -> None:
    """
    Public entry point — never raises. Every failure mode inside is either
    a normal no-op (see module docstring) or caught here so a bug in this
    module can never propagate into the caller's request/background task.
    """
    try:
        _notify(batch_id)
    except Exception as exc:
        # type(exc).__name__ only, same rule as telegram_bot.py -- an
        # exception here could in principle wrap a requests error that
        # embeds the bot token in its own str().
        logger.warning(
            "[streamer_notify] unexpected_error batch_id=%s error_type=%s",
            batch_id, type(exc).__name__,
        )


def _notify(batch_id: str) -> None:
    batch = get_streamer_batch_by_id(batch_id)
    if not batch:
        return
    if batch["status"] not in _ELIGIBLE_STATUSES:
        return  # failed/cancelled/still in progress -- never notify for these
    if batch.get("telegram_notified_at") is not None:
        return  # already sent -- cheap local short-circuit before the claim

    # preset_snapshot is fixed at batch-creation time (POST /api/streamer/
    # batches) and never mutated afterward by any code path -- no race to
    # worry about reading it here without a lock.
    preset = batch.get("preset_snapshot") or {}
    if not preset.get("telegram_notify_enabled"):
        return  # this batch's own creation-time decision was "no" -- final

    user = auth_store.get_user_by_id(batch["user_id"])
    if not user or not user.get("telegram_linked") or not user.get("telegram_chat_id"):
        # Covers both "never linked" and "was linked, unlinked before
        # completion" -- current live state, read fresh right now, is
        # what decides this. A user linked AFTER batch creation but
        # BEFORE this runs is exactly why this is a live read and not a
        # value captured at creation time.
        return

    if not claim_streamer_notification(batch_id):
        return  # already sent by a racer, or another process holds the active claim

    message = _MSG_READY if batch["status"] == STREAMER_BATCH_STATUS_READY else _MSG_PARTIALLY_FAILED
    deep_link = f"{SITE_URL}/streamer-mode.html?batch={batch_id}"

    sent = telegram_bot.send_message(
        user["telegram_chat_id"], message, button_text=_BUTTON_TEXT, button_url=deep_link,
    )
    if sent:
        mark_streamer_notification_sent(batch_id)
    else:
        mark_streamer_notification_failed(batch_id, "telegram_send_failed")
