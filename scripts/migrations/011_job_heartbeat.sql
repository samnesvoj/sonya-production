-- Migration 011: heartbeat_at column for long-running jobs
--
-- Problem: requeue_stale_jobs() (scripts/prod_job_store.py) treats any
-- active-status job as stuck once `claimed_at` is older than
-- `stale_minutes` (default 30). `claimed_at` is set once at claim time and
-- never refreshed by status updates -- a job that legitimately spends more
-- than 30 minutes inside a single status (e.g. streamer mode's
-- enrich_video_for_mode() transcribing a multi-hour VOD) risks being
-- requeued out from under a worker that is still alive and working.
--
-- Fix: a dedicated, purely-additive liveness column the worker refreshes
-- periodically during long operations (see gpu_worker.py's heartbeat
-- thread), independent of `status`/`updated_at` -- this never changes what
-- status means, only whether the worker is still alive.
--
-- Nullable, no backfill needed: requeue_stale_jobs() falls back to the
-- existing claimed_at-only check via COALESCE(heartbeat_at, claimed_at)
-- (see prod_job_store.py), so every existing row and every job whose
-- worker never calls the new heartbeat function behaves exactly as before
-- this migration.

ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
