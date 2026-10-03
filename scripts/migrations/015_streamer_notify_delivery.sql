-- Migration 015: delivery state for @sonya_group_bot completion notifications
--
-- streamer_batches.telegram_notified_at already exists (migration 012) as
-- the once-only "has this been sent" guard, but that column alone isn't
-- enough to make the SEND itself safe under concurrent reconciliation --
-- two processes could both see telegram_notified_at IS NULL and both call
-- Telegram's sendMessage. These three columns add a lease-based atomic
-- claim on top of it (see scripts/streamer_notify.py's claim_streamer_
-- notification() / mark_streamer_notification_sent() / mark_streamer_
-- notification_failed()) -- same pattern as telegram_store.claim_update()'s
-- lease (migration 013) and set_streamer_batch_status()'s own atomic
-- conditional UPDATE.
--
-- telegram_notify_claimed_at also doubles as the retry backoff: a FAILED
-- attempt does NOT clear it, so the next claim attempt only succeeds once
-- the lease itself has expired -- this is what keeps a repeated GET/
-- reconcile from hammering the Telegram API on every request after a
-- transient failure, without needing a separate cooldown column or a
-- scheduler.
--
-- No BEGIN/COMMIT -- run_migrations.py already executes each file's full
-- text inside a single implicit transaction, and every file in
-- scripts/migrations/ is re-executed on every deploy (no migration-
-- tracking table), so this must stay idempotent (IF NOT EXISTS
-- everywhere), matching every migration before it.

ALTER TABLE streamer_batches
    ADD COLUMN IF NOT EXISTS telegram_notify_claimed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS telegram_notify_attempts     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS telegram_notify_last_error   TEXT;
