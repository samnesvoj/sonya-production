-- Migration 012: streamer_batches / streamer_segments / streamer_clip_jobs
--
-- Batch foundation for the long-form two-phase streamer architecture (see
-- PHASE A audit + modes/streamer/runner.py's analyze()/compose_one()
-- split): one batch = one source video, analyzed once, with N selected
-- segments each becoming its own lightweight generation_jobs row via the
-- streamer_clip_jobs join table below.
--
-- Deliberately does NOT touch generation_jobs at all -- that table's
-- worker-claim/status/idempotency contract is load-bearing production
-- code; batch-awareness is added entirely as a side table instead of a
-- new column or FK direction on generation_jobs.
--
-- No separate `sources` table: a batch's source is its analysis job's own
-- s3_input_key (generation_jobs.analysis_job_id, nullable -- a batch can
-- exist during ingest, before any generation job has been created yet).
--
-- No Telegram linking columns here (telegram_chat_id / telegram_user_id /
-- link tokens / webhook tables) -- that is account-level, not batch-level,
-- and is a separate future migration so this one stays batch-schema-only.
-- telegram_notified_at IS added now: it is intrinsically a batch-level
-- idempotency guard ("has this batch's completion already been announced
-- once"), not an account/linking concern, and every future notification
-- sender needs it to exist regardless of when Telegram linking itself
-- lands.
--
-- No expires_at / TTL / cleanup here either -- that is a separate storage
-- lifecycle migration, deliberately not mixed with this one.
--
-- No BEGIN/COMMIT -- run_migrations.py already executes each file's full
-- text inside a single implicit transaction, and every file in
-- scripts/migrations/ is re-executed on every deploy (no migration-
-- tracking table), so this must stay idempotent (IF NOT EXISTS
-- everywhere), matching every migration before it.
--
-- id columns are UUID but, like users/sessions/payments before them,
-- always generated in application code (str(uuid.uuid4())) and inserted
-- explicitly -- never DB-generated. analysis_job_id / streamer_clip_jobs.
-- job_id are TEXT, matching generation_jobs.id's own (pre-existing,
-- unchanged) type.

CREATE TABLE IF NOT EXISTS streamer_batches (
    id                    UUID        PRIMARY KEY,
    user_id               UUID        NOT NULL REFERENCES users(id),
    -- Nullable by design: a batch is created at the start of ingest, before
    -- the analysis generation_jobs row necessarily exists yet, and is
    -- attached afterward (see attach_analysis_job_to_batch() in
    -- prod_job_store.py). No ON DELETE CASCADE/SET NULL here — generation_jobs
    -- rows are never deleted by any existing code path, so the question is
    -- moot in practice, and the default NO ACTION is the safer choice
    -- (never silently orphans or nulls out a batch's own source reference).
    analysis_job_id       TEXT        REFERENCES generation_jobs(id),
    preset_snapshot       JSONB       NOT NULL DEFAULT '{}',
    status                TEXT        NOT NULL DEFAULT 'queued'
                          CHECK (status IN (
                              'queued',
                              'ingesting',
                              'analyzing',
                              'awaiting_selection',
                              'generating',
                              'ready',
                              'partially_failed',
                              'failed',
                              'cancelled'
                          )),
    error                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at          TIMESTAMPTZ,
    -- Idempotency guard for the future @sonya_group_bot completion
    -- notification (see IMPORTANT — TELEGRAM in the audit): at most one
    -- notification per batch, checked-and-set by whatever eventually sends
    -- it. Added now on purpose even though nothing writes it yet.
    telegram_notified_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_streamer_batches_user_created
    ON streamer_batches (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_streamer_batches_status
    ON streamer_batches (status);

CREATE INDEX IF NOT EXISTS idx_streamer_batches_analysis_job_id
    ON streamer_batches (analysis_job_id)
    WHERE analysis_job_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_streamer_batches_updated_at ON streamer_batches;
CREATE TRIGGER trg_streamer_batches_updated_at
    BEFORE UPDATE ON streamer_batches
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- Canonical segment contract: start_sec + duration_sec ONLY (see
-- modes/streamer/runner.py's own module docstring for the same rule) --
-- no end_sec/start_time/end_time/offset column. Any analyzer output in a
-- different shape is normalized to this before the INSERT, not in here.
CREATE TABLE IF NOT EXISTS streamer_segments (
    id             UUID        PRIMARY KEY,
    batch_id       UUID        NOT NULL REFERENCES streamer_batches(id) ON DELETE CASCADE,
    -- Display/selection order within a batch — analyzer output order, not
    -- a ranking (score carries ranking; see PHASE A analyze()).
    ordinal        INTEGER     NOT NULL,
    start_sec      DOUBLE PRECISION NOT NULL,
    duration_sec   DOUBLE PRECISION NOT NULL,
    title          TEXT        NOT NULL,
    description    TEXT,
    score          DOUBLE PRECISION,
    recommended    BOOLEAN     NOT NULL DEFAULT FALSE,
    selected       BOOLEAN     NOT NULL DEFAULT TRUE,
    -- crop_hints: analyze()'s own output, persisted so a later compose_one()
    -- job reuses it instead of re-running enrich_video_for_mode() — the
    -- entire point of the two-phase split (see PHASE A audit).
    crop_hints     JSONB       NOT NULL DEFAULT '{}',
    -- Free-form analyzer diagnostics (e.g. `source: "speaker_fallback"`) —
    -- deliberately separate from crop_hints so callers that only need crop
    -- data don't have to know this key's shape.
    metadata       JSONB       NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_streamer_segments_batch_id
    ON streamer_segments (batch_id);

-- Also serves as the natural "list segments in order" index — covers
-- both the batch_id-only lookup and the batch_id+ordinal ordered scan.
CREATE UNIQUE INDEX IF NOT EXISTS ux_streamer_segments_batch_ordinal
    ON streamer_segments (batch_id, ordinal);

DROP TRIGGER IF EXISTS trg_streamer_segments_updated_at ON streamer_segments;
CREATE TRIGGER trg_streamer_segments_updated_at
    BEFORE UPDATE ON streamer_segments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- Thin join table only — no independent surrogate id, no columns beyond
-- what the relation itself needs. segment_id as PRIMARY KEY already
-- enforces "one job per segment"; UNIQUE(job_id) enforces the reverse
-- ("one segment per job"), together making this a strict 1:1 join.
-- job_id -> generation_jobs has no ON DELETE action: deleting a batch (and
-- its segments, and therefore this join row, via the CASCADEs above) must
-- never reach across to a real generation_jobs row -- and since no FK runs
-- in that direction at all, it structurally can't.
CREATE TABLE IF NOT EXISTS streamer_clip_jobs (
    batch_id     UUID        NOT NULL REFERENCES streamer_batches(id) ON DELETE CASCADE,
    segment_id   UUID        PRIMARY KEY REFERENCES streamer_segments(id) ON DELETE CASCADE,
    job_id       TEXT        NOT NULL UNIQUE REFERENCES generation_jobs(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- segment_id (PRIMARY KEY) and job_id (UNIQUE) are already indexed by
-- their own constraints -- only batch_id needs an explicit index here.
CREATE INDEX IF NOT EXISTS idx_streamer_clip_jobs_batch_id
    ON streamer_clip_jobs (batch_id);
