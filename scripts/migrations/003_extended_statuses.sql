-- Migration 003: extend job statuses, add retry and quota columns

-- Add retry tracking
ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS retry_count    INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_retries    INTEGER     NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS last_error     TEXT,
    ADD COLUMN IF NOT EXISTS queue_priority INTEGER     NOT NULL DEFAULT 0;

-- Add enrichment metadata
ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS enrichment_keys  TEXT[]  DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS clip_count       INTEGER,
    ADD COLUMN IF NOT EXISTS processing_ms    INTEGER;

-- Extended status constraint (in case migration 001 ran with old statuses)
ALTER TABLE generation_jobs
    DROP CONSTRAINT IF EXISTS generation_jobs_status_check;

ALTER TABLE generation_jobs
    ADD CONSTRAINT generation_jobs_status_check
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
    ));

-- Index for priority queue ordering
CREATE INDEX IF NOT EXISTS idx_jobs_priority_queue
    ON generation_jobs (status, queue_priority DESC, created_at ASC)
    WHERE status = 'queued';

-- Stale jobs view: claimed/running jobs stuck > 30 minutes
CREATE OR REPLACE VIEW stale_jobs AS
    SELECT id, user_id, mode, retry_count, max_retries, claimed_at, status
    FROM generation_jobs
    WHERE status IN ('claimed','downloading','model_downloading','mode_running',
                     'analyzing','yolo','scripting','tts','subtitles',
                     'assembling','uploading_result','worker_started')
      AND claimed_at < NOW() - INTERVAL '30 minutes';
