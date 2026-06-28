-- Migration 006: GPU queue priority + ephemeral worker tracking
-- Adds dispatcher/ephemeral-GPU scheduling columns to generation_jobs.
-- All new columns use IF NOT EXISTS — safe to re-run.
-- Note: completed_at already exists from migration 001; skipped here.

BEGIN;

ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS priority             INTEGER     NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS plan                 TEXT,
    ADD COLUMN IF NOT EXISTS gpu_status           TEXT,
    ADD COLUMN IF NOT EXISTS attempts             INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts         INTEGER     NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS locked_until         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS queued_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS gpu_requested_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS worker_started_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS failed_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS orchestrator_payload JSONB,
    ADD COLUMN IF NOT EXISTS orchestrator_error   TEXT;

-- Back-fill queued_at from created_at for existing rows
UPDATE generation_jobs
SET queued_at = created_at
WHERE queued_at IS NULL AND created_at IS NOT NULL;

-- Index: dispatcher queue scan (hot path — priority desc, then FIFO)
CREATE INDEX IF NOT EXISTS ix_jobs_dispatch_priority
    ON generation_jobs (status, priority DESC, queued_at ASC)
    WHERE status = 'queued';

-- Index: lock expiry scan (dispatcher retries stale locks)
CREATE INDEX IF NOT EXISTS ix_jobs_locked_until
    ON generation_jobs (locked_until)
    WHERE locked_until IS NOT NULL;

-- Index: gpu_status monitoring / alerting
CREATE INDEX IF NOT EXISTS ix_jobs_gpu_status
    ON generation_jobs (gpu_status)
    WHERE gpu_status IS NOT NULL;

COMMIT;
