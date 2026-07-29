-- Migration 009: idempotency key for generation job creation
--
-- Prevents duplicate job creation from concurrent or retried
-- POST /api/generation/jobs requests carrying the same client-supplied
-- Idempotency-Key header (see scripts/prod_generation_api.py and
-- scripts/prod_job_store.create_job_idempotent()).
--
-- No BEGIN/COMMIT here: run_migrations.py already executes each file's
-- full text inside a single implicit transaction (`with conn: cur.execute
-- (sql)`), and there is no migration-tracking table -- every file in
-- scripts/migrations/ is re-executed on every deploy, so this must stay
-- idempotent (IF NOT EXISTS everywhere) rather than rely on running once.
--
-- Nullable by design: existing rows and any request that omits the
-- Idempotency-Key header get NULL for both columns. PostgreSQL treats
-- every NULL as distinct from every other value (including other NULLs)
-- in a UNIQUE index, so this needs no backfill and stays fully backward
-- compatible with every existing row and with clients that don't send
-- the header yet -- no partial index needed.
--
-- Key length/format is validated at the application layer
-- (scripts/prod_generation_api.py), not via a DB CHECK constraint, to
-- avoid a DROP CONSTRAINT + ADD CONSTRAINT pair re-running (and briefly
-- dropping the constraint) on every deploy.

ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key        TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_fingerprint TEXT;

-- Composite UNIQUE index, not partial: scoped by user_id so two different
-- users may coincidentally reuse the same client-generated key value
-- without colliding. Not CONCURRENTLY -- incompatible with the
-- transactional execution above, and generation_jobs is small at this
-- stage (single manually-managed GPU worker); see the idempotency design
-- doc for the rollout risk if the table grows substantially before this
-- migration is applied.
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_user_idempotency_key
    ON generation_jobs (user_id, idempotency_key);
