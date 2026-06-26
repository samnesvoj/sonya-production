-- Migration 002: generation_files table
-- Tracks all S3 files associated with a job (inputs, outputs, previews)

CREATE TABLE IF NOT EXISTS generation_files (
    id           TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    job_id       TEXT        NOT NULL REFERENCES generation_jobs(id) ON DELETE CASCADE,
    user_id      TEXT        NOT NULL,
    file_type    TEXT        NOT NULL
                             CHECK (file_type IN ('input','output','preview','subtitle','audio','thumbnail')),
    s3_key       TEXT        NOT NULL,
    filename     TEXT        NOT NULL,
    content_type TEXT        NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   BIGINT,
    duration_sec FLOAT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_files_job_id
    ON generation_files (job_id, file_type);

CREATE INDEX IF NOT EXISTS idx_files_user_id
    ON generation_files (user_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_s3_key
    ON generation_files (s3_key);
