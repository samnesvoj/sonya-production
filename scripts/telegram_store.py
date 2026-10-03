"""
telegram_store.py
==================
PostgreSQL data access layer for Telegram account linking (migration 013).

Tables:
  users (telegram_user_id / telegram_chat_id / telegram_linked /
         telegram_linked_at columns) — the linked identity itself
  telegram_link_tokens — hashed one-time linking tokens
  telegram_updates     — webhook update_id idempotency guard

All functions open/close their own connection (matches auth_store.py /
payment_store.py style — low-QPS path, not the hot job-processing path).

No raw link tokens, bot token, or webhook secret are ever persisted,
logged, or returned by this module beyond create_link_token()'s single
return value (which the caller — POST /api/telegram/link-token — sends to
the browser exactly once and never logs).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from scripts.telegram_security import generate_link_token, hash_link_token, link_token_expiry

logger = logging.getLogger(__name__)

_DB_AVAILABLE = False
try:
    import psycopg2
    import psycopg2.extras
    _DB_AVAILABLE = True
except ImportError:
    pass


def _get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    if not _DB_AVAILABLE:
        raise RuntimeError("psycopg2 not installed — run: pip install psycopg2-binary")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(conn, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


# ── Domain exceptions ────────────────────────────────────────────────────

class TelegramLinkTokenInvalidError(ValueError):
    """No such token exists (never issued, or a typo/garbage value)."""


class TelegramLinkTokenExpiredError(ValueError):
    """Token exists, was never used, but its TTL has passed."""


class TelegramLinkTokenUsedError(ValueError):
    """Token exists and was already consumed — a replay attempt."""


class TelegramAccountAlreadyLinkedError(ValueError):
    """This SONYA user_id already has a DIFFERENT Telegram account linked
    — an explicit unlink is required before linking a new one."""


class TelegramAlreadyLinkedToAnotherUserError(ValueError):
    """This telegram_user_id is already linked to a DIFFERENT SONYA
    account — enforced by ux_users_telegram_user_id (migration 013)."""


# ── Link tokens ──────────────────────────────────────────────────────────

def create_link_token(user_id: str) -> Dict[str, Any]:
    """
    Issues a new one-time link token for user_id. Returns
    {"token": <raw>, "id": ..., "expires_at": ...} — `token` is the ONLY
    place the raw value ever appears; only its hash is written to
    telegram_link_tokens.

    Small abuse guard (no separate rate limiter): every still-unused token
    this user already holds is invalidated first, in the same transaction,
    by pulling its expires_at back to now — reusing the existing expiry
    check rather than inventing a new state. This keeps at most one live
    deep link per user instead of letting them accumulate indefinitely; a
    stale link a user already opened just silently stops working the
    moment they ask for a new one, same as it would once its own TTL
    passed anyway.
    """
    raw_token = generate_link_token()
    token_hash = hash_link_token(raw_token)
    token_id = str(uuid.uuid4())
    expires_at = link_token_expiry()
    now = _now()

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE telegram_link_tokens
                    SET expires_at = LEAST(expires_at, %s)
                    WHERE user_id = %s AND used_at IS NULL
                    """,
                    (now, user_id),
                )
                cur.execute(
                    """
                    INSERT INTO telegram_link_tokens
                        (id, user_id, token_hash, created_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (token_id, user_id, token_hash, now, expires_at),
                )
    finally:
        conn.close()

    return {"token": raw_token, "id": token_id, "expires_at": expires_at}


def consume_link_token(raw_token: str) -> str:
    """
    Atomically marks a link token used and returns its user_id.

    The used_at stamp happens in the SAME UPDATE that checks
    `used_at IS NULL AND expires_at > now` — not a SELECT-then-UPDATE — so
    two concurrent redemptions of the same raw token (e.g. Telegram
    redelivering the webhook, or a user double-tapping Start) can never
    both succeed; the loser's UPDATE affects 0 rows and the diagnostic
    SELECT below only decides which error to raise, never whether the
    consumption happens (same pattern as prod_job_store.
    set_streamer_batch_status()'s terminal-transition guard).

    Raises TelegramLinkTokenInvalidError / ...ExpiredError / ...UsedError.
    A token that is BOTH expired AND already used reports "used" — the
    more specific, more recent-truth condition.
    """
    token_hash = hash_link_token(raw_token)
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE telegram_link_tokens
                    SET used_at = %s
                    WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s
                    RETURNING user_id
                    """,
                    (_now(), token_hash, _now()),
                )
                row = cur.fetchone()
                if row:
                    return str(row["user_id"])

                existing = _row(
                    conn, "SELECT * FROM telegram_link_tokens WHERE token_hash=%s", (token_hash,)
                )
                if not existing:
                    raise TelegramLinkTokenInvalidError("unknown token")
                if existing["used_at"] is not None:
                    raise TelegramLinkTokenUsedError("token already used")
                raise TelegramLinkTokenExpiredError("token expired")
    finally:
        conn.close()


# ── Account linking ──────────────────────────────────────────────────────

def link_telegram_account(user_id: str, telegram_user_id: int, telegram_chat_id: int) -> Dict[str, Any]:
    """
    Links a Telegram identity to a SONYA account. Refuses to silently
    re-link either side (see the two exceptions below) — an explicit
    unlink_telegram_account() must happen first in either case.

    Re-linking the SAME (user_id, telegram_user_id) pair (e.g. a retried
    webhook delivery after the DB write already landed) is idempotent —
    telegram_linked_at is preserved via COALESCE, not bumped.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_user_id FROM users WHERE id=%s", (user_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"user not found: {user_id}")
                current_tg_id = row["telegram_user_id"]
                if current_tg_id is not None and current_tg_id != telegram_user_id:
                    raise TelegramAccountAlreadyLinkedError(
                        f"SONYA user {user_id} is already linked to a different Telegram account"
                    )

                try:
                    cur.execute(
                        """
                        UPDATE users
                        SET telegram_user_id = %s,
                            telegram_chat_id = %s,
                            telegram_linked = TRUE,
                            telegram_linked_at = COALESCE(telegram_linked_at, %s),
                            updated_at = %s
                        WHERE id = %s
                        RETURNING *
                        """,
                        (telegram_user_id, telegram_chat_id, _now(), _now(), user_id),
                    )
                except psycopg2.IntegrityError:
                    # ux_users_telegram_user_id (migration 013) — this
                    # telegram_user_id belongs to a different user row.
                    # The transaction is now aborted; re-raise as our own
                    # domain exception, let `with conn:` roll back.
                    raise TelegramAlreadyLinkedToAnotherUserError(
                        f"Telegram account {telegram_user_id} is already linked to a different SONYA user"
                    )
                return dict(cur.fetchone())
    finally:
        conn.close()


def link_via_token(raw_token: str, telegram_user_id: int, telegram_chat_id: int) -> Dict[str, Any]:
    """
    Atomically consumes a link token AND links the account it belongs to,
    in ONE DB transaction. This is the function the webhook uses — NOT
    consume_link_token() + link_telegram_account() called back to back.

    Why: with two separate transactions, a transient failure between them
    (the process dying, a DB connection drop) could leave a token marked
    used with no account actually linked — permanently unusable, no
    retry possible, no visible error. Inside one transaction, ANY
    exception raised anywhere in here — a transient DB error, or one of
    our own domain exceptions below — rolls back the ENTIRE transaction,
    including the token's used_at stamp. So:
      - token becomes used ONLY together with a successful link
      - a conflict (already linked, either direction) is a clean,
        specific exception, and leaves the token unconsumed — a later
        /start (same token, once the conflict is resolved, e.g. after an
        unlink) can still redeem it
      - a transient failure leaves the token exactly as it was — safely
        retryable, nothing "destroyed"
      - a token that WAS already used by a prior successful call is still
        rejected the normal way (TelegramLinkTokenUsedError) — repeated
        successful use stays impossible

    consume_link_token() / link_telegram_account() above are kept as
    separate, independently usable/testable primitives — this function
    doesn't replace them, it composes the same two operations atomically
    for the one caller (the webhook) that needs that guarantee.
    """
    token_hash = hash_link_token(raw_token)
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                # ── 1. Consume the token ──────────────────────────────────
                cur.execute(
                    """
                    UPDATE telegram_link_tokens
                    SET used_at = %s
                    WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s
                    RETURNING user_id
                    """,
                    (_now(), token_hash, _now()),
                )
                token_row = cur.fetchone()
                if not token_row:
                    existing = _row(
                        conn, "SELECT * FROM telegram_link_tokens WHERE token_hash=%s", (token_hash,)
                    )
                    if not existing:
                        raise TelegramLinkTokenInvalidError("unknown token")
                    if existing["used_at"] is not None:
                        raise TelegramLinkTokenUsedError("token already used")
                    raise TelegramLinkTokenExpiredError("token expired")
                user_id = str(token_row["user_id"])

                # ── 2. Link, in the SAME transaction — any exception here
                #    (including the two below) rolls step 1 back too. ─────
                cur.execute("SELECT telegram_user_id FROM users WHERE id=%s", (user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    raise ValueError(f"user not found: {user_id}")
                current_tg_id = user_row["telegram_user_id"]
                if current_tg_id is not None and current_tg_id != telegram_user_id:
                    raise TelegramAccountAlreadyLinkedError(
                        f"SONYA user {user_id} is already linked to a different Telegram account"
                    )

                try:
                    cur.execute(
                        """
                        UPDATE users
                        SET telegram_user_id = %s,
                            telegram_chat_id = %s,
                            telegram_linked = TRUE,
                            telegram_linked_at = COALESCE(telegram_linked_at, %s),
                            updated_at = %s
                        WHERE id = %s
                        RETURNING *
                        """,
                        (telegram_user_id, telegram_chat_id, _now(), _now(), user_id),
                    )
                except psycopg2.IntegrityError:
                    raise TelegramAlreadyLinkedToAnotherUserError(
                        f"Telegram account {telegram_user_id} is already linked to a different SONYA user"
                    )
                return dict(cur.fetchone())
    finally:
        conn.close()


def unlink_telegram_account(user_id: str) -> None:
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET telegram_linked = FALSE,
                        telegram_user_id = NULL,
                        telegram_chat_id = NULL,
                        telegram_linked_at = NULL,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (_now(), user_id),
                )
    finally:
        conn.close()


# ── Webhook idempotency ──────────────────────────────────────────────────
#
# Two-phase, not a single INSERT ... DO NOTHING: marking an update
# "processed" the moment it's RECEIVED (the original version of this
# module) means a delivery whose handler then throws is never retryable —
# Telegram's own redelivery would find the update already marked done and
# skip it, silently losing whatever /start it carried. claim_update() only
# records that an attempt is starting; mark_update_processed() is the
# caller's job to call ONLY after the handler actually succeeds.

# How long a claim is considered "actively being worked on" before another
# request is allowed to retry it — comfortably longer than a webhook
# request should ever take (no external calls beyond one Telegram
# sendMessage), short enough that a genuinely failed attempt doesn't block
# a legitimate retry for long.
_UPDATE_CLAIM_LEASE_SECONDS = 30


def claim_update(update_id: int) -> str:
    """
    Atomically decides what the caller should do with this update_id:

      "process" — go ahead and run the handler, then call
                  mark_update_processed() on success. Covers a brand-new
                  update_id, and a previous attempt whose claim has
                  expired without ever being marked processed (i.e. it
                  failed or crashed).
      "skip"    — do nothing and return success without running the
                  handler. Either already fully processed (the common
                  case: Telegram redelivering after a successful 200), or
                  another request claimed it within the last
                  _UPDATE_CLAIM_LEASE_SECONDS and is presumably still
                  handling it right now.

    Race-safe by construction: the "new update" path is a single
    INSERT ... ON CONFLICT DO NOTHING, and the "reclaim an existing,
    unprocessed, expired-lease row" path is a single conditional UPDATE
    (decision and mutation together, same pattern as prod_job_store.
    set_streamer_batch_status()'s terminal-transition guard) — never a
    SELECT that decides before a separate UPDATE acts. Two genuinely
    concurrent claim_update() calls for the SAME update_id can't both get
    "process": Postgres serializes the conflicting INSERT/UPDATE via the
    primary key, so only one commits a fresh claimed_at; the other's
    statement affects 0 rows and it gets "skip".
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                now = _now()
                cur.execute(
                    """
                    INSERT INTO telegram_updates (update_id, received_at, claimed_at, processed_at)
                    VALUES (%s, %s, %s, NULL)
                    ON CONFLICT (update_id) DO NOTHING
                    RETURNING update_id
                    """,
                    (update_id, now, now),
                )
                if cur.fetchone():
                    return "process"

                cur.execute(
                    """
                    UPDATE telegram_updates
                    SET claimed_at = %s
                    WHERE update_id = %s
                      AND processed_at IS NULL
                      AND (claimed_at IS NULL OR claimed_at < %s)
                    RETURNING update_id
                    """,
                    (now, update_id, now - timedelta(seconds=_UPDATE_CLAIM_LEASE_SECONDS)),
                )
                if cur.fetchone():
                    return "process"
                return "skip"
    finally:
        conn.close()


def mark_update_processed(update_id: int) -> None:
    """Call ONLY after the handler for this update_id has fully succeeded
    — see claim_update()'s docstring for why this is a separate step."""
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE telegram_updates SET processed_at=%s WHERE update_id=%s",
                    (_now(), update_id),
                )
    finally:
        conn.close()
