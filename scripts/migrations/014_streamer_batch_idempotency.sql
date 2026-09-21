-- Migration 014: idempotency key for streamer batch creation
--
-- Prevents a double-click or network retry on POST /api/streamer/batches
-- from creating two streamer_batches rows (and charging Free-plan quota
-- twice) for what the user experiences as one submission. Same pattern as
-- migration 009's generation_jobs.idempotency_key -- see
-- scripts/prod_job_store.create_streamer_batch() and
-- scripts/streamer_routes.py's POST /api/streamer/batches.
--
-- No BEGIN/COMMIT here: run_migrations.py already executes each file's
-- full text inside a single implicit transaction, and there is no
-- migration-tracking table -- every file in scripts/migrations/ is
-- re-executed on every deploy, so this must stay idempotent (IF NOT
-- EXISTS everywhere) rather than rely on running once.
--
-- Nullable by design, same reasoning as migration 009: NULL is distinct
-- from NULL under UNIQUE, so a request that omits Idempotency-Key (legacy
-- behavior -- always a new batch) never collides with anything, and no
-- backfill is needed for existing rows.

ALTER TABLE streamer_batches
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_streamer_batches_user_idempotency_key
    ON streamer_batches (user_id, idempotency_key);
