-- Migration 001: generation_jobs table
-- Run once on initial deploy: psql $DATABASE_URL -f scripts/migrations/001_generation_jobs.sql

CREATE TABLE IF NOT EXISTS generation_jobs (
    id               TEXT        PRIMARY KEY,
    user_id          TEXT        NOT NULL,
    mode             TEXT        NOT NULL,
    params           JSONB       NOT NULL DEFAULT '{}',
    s3_input_key     TEXT        NOT NULL,
    s3_output_key    TEXT,
    status           TEXT        NOT NULL DEFAULT 'queued'
                                 CHECK (status IN (
                                     'queued',
                                     'claimed',
                                     'gpu_requested',
                                     'gpu_booting',
                                     'worker_started',
                                     'downloading',
                                     'model_downloading',
                                     'mode_running',
                                     'analyzing',
                                     'yolo',
                                     'scripting',
                                     'tts',
                                     'subtitles',
                                     'assembling',
                                     'uploading_result',
                                     'completed',
                                     'failed',
                                     'cancelled'
                                 )),
    error            TEXT,
    worker_id        TEXT,
    claimed_at       TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON generation_jobs (status, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_jobs_user_id
    ON generation_jobs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_jobs_worker_id
    ON generation_jobs (worker_id)
    WHERE worker_id IS NOT NULL;

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobs_updated_at ON generation_jobs;
CREATE TRIGGER trg_jobs_updated_at
    BEFORE UPDATE ON generation_jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
