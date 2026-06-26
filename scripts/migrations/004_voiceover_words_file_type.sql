-- Migration 004: add voiceover and words_json file types

-- Extend file_type enum to support additional output types
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
        'enrichment_json'
    ));

-- Add word-level transcript storage column on jobs
ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS word_timestamps_s3_key TEXT,
    ADD COLUMN IF NOT EXISTS subtitle_s3_key         TEXT;
