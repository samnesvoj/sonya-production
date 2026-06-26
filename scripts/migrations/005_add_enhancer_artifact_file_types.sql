-- Migration 005: enhancer artifact file types and audit log

-- Add more enrichment artifact types
ALTER TABLE generation_files
    DROP CONSTRAINT IF EXISTS generation_files_file_type_check;

ALTER TABLE generation_files
    ADD CONSTRAINT generation_files_file_type_check
    CHECK (file_type IN (
        'input',
        'output',
        'preview',
        'subtitle',
        'audio',
        'thumbnail',
        'voiceover',
        'words_json',
        'enrichment_json',
        'gemini_analysis',
        'layout_analysis',
        'yolo_detections',
        'crop_hints'
    ));

-- Audit log for security events
CREATE TABLE IF NOT EXISTS security_audit_log (
    id           BIGSERIAL   PRIMARY KEY,
    event_type   TEXT        NOT NULL,
    user_id      TEXT,
    job_id       TEXT,
    ip_address   TEXT,
    user_agent   TEXT,
    trace_id     TEXT,
    details      JSONB       DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_user_id
    ON security_audit_log (user_id, created_at DESC)
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_event_type
    ON security_audit_log (event_type, created_at DESC);

-- Rate limit tracking
CREATE TABLE IF NOT EXISTS rate_limit_counters (
    key          TEXT        NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count        INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_window
    ON rate_limit_counters (key, window_start DESC);
