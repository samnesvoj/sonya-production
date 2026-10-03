-- Migration 013: Telegram account linking (@sonya_group_bot)
--
-- Account-level linking foundation only -- no notification sending yet
-- (see scripts/telegram_bot.py / telegram_routes.py: the webhook only
-- ever replies to /start, nothing here triggers a streamer-completion
-- message). streamer_batches.telegram_notified_at (migration 012) already
-- exists as that future sender's idempotency guard; this migration is
-- deliberately kept separate so batch schema and Telegram identity schema
-- never mix.
--
-- users.telegram_linked (migration 008) is KEPT, and from this migration
-- on actually reflects real linking state -- previously nothing ever set
-- it to true.
--
-- No BEGIN/COMMIT -- run_migrations.py already executes each file's full
-- text inside a single implicit transaction, and every file in
-- scripts/migrations/ is re-executed on every deploy (no migration-
-- tracking table), so this must stay idempotent (IF NOT EXISTS
-- everywhere), matching every migration before it.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS telegram_user_id   BIGINT,
    ADD COLUMN IF NOT EXISTS telegram_chat_id   BIGINT,
    ADD COLUMN IF NOT EXISTS telegram_linked_at TIMESTAMPTZ;

-- One Telegram account can never be linked to two SONYA accounts at once.
-- Partial (WHERE NOT NULL) so the many users with no Telegram link don't
-- collide on NULL -- same idiom as
-- idx_streamer_batches_analysis_job_id (migration 012). This is the real
-- enforcement point for "Telegram account already linked to another SONYA
-- account" -- scripts/telegram_store.py::link_telegram_account() checks
-- it up front for a clean error, but this index is what actually
-- guarantees it under concurrent linking attempts.
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_telegram_user_id
    ON users (telegram_user_id)
    WHERE telegram_user_id IS NOT NULL;

-- One-time linking tokens. Only token_hash is ever stored -- the raw
-- token is returned once, by POST /api/telegram/link-token, embedded in
-- the t.me deep link, and never persisted anywhere (see
-- telegram_security.py, which mirrors auth_security.py's session-token
-- pattern: HMAC-SHA256 keyed by the existing AUTH_SECRET -- no new secret
-- invented just for this).
CREATE TABLE IF NOT EXISTS telegram_link_tokens (
    id          UUID        PRIMARY KEY,
    user_id     UUID        NOT NULL REFERENCES users(id),
    token_hash  TEXT        NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_telegram_link_tokens_user_id
    ON telegram_link_tokens (user_id, created_at DESC);

-- Webhook idempotency: Telegram may redeliver the same update (at-least-
-- once delivery), so POST /api/telegram/webhook must be safe to receive
-- the same update_id twice -- AND a handler that fails partway through
-- must still be retryable on the next redelivery, not permanently marked
-- done. Two nullable timestamps besides received_at capture that:
--   claimed_at    -- an attempt is in progress (or finished); reclaimable
--                    once stale (see telegram_store._UPDATE_CLAIM_LEASE_SECONDS)
--   processed_at  -- set ONLY after the handler actually succeeds
-- See telegram_store.claim_update() / mark_update_processed() for the
-- actual dedup + retry-after-failure logic this backs.
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id     BIGINT      PRIMARY KEY,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at    TIMESTAMPTZ,
    processed_at  TIMESTAMPTZ
);
