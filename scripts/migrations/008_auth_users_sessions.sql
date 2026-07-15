-- Migration 008: passwordless email auth — users, auth_codes, sessions
--
-- Design notes:
--   * `id` columns are UUID but are always generated in application code
--     (uuid.uuid4()) and inserted explicitly, matching the existing
--     generation_jobs pattern — no dependency on pgcrypto/uuid-ossp.
--   * `users.plan_active_until` and `users.telegram_linked` are not in the
--     original spec's column list but are required by the frontend's
--     GET /api/auth/me response contract, so they are added here.
--   * `token_hash` / `code_hash` store only HMAC-SHA256 digests — raw
--     session tokens and raw 6-digit codes are never persisted.
--
-- All new objects use IF NOT EXISTS — safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id                 UUID        PRIMARY KEY,
    email              TEXT        NOT NULL,
    email_verified_at  TIMESTAMPTZ,
    plan_type          TEXT        NOT NULL DEFAULT 'free',
    plan_status        TEXT        NOT NULL DEFAULT 'active',
    plan_active_until  TIMESTAMPTZ,
    free_video_limit   INTEGER     NOT NULL DEFAULT 1,
    free_video_used    INTEGER     NOT NULL DEFAULT 0,
    telegram_linked    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Case-insensitive uniqueness on email (emails are normalized to lowercase
-- in application code before every read/write, but the unique index is the
-- authoritative guard against races / bypasses).
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_lower
    ON users (LOWER(email));

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE IF NOT EXISTS auth_codes (
    id           UUID        PRIMARY KEY,
    email        TEXT        NOT NULL,
    code_hash    TEXT        NOT NULL,
    purpose      TEXT        NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    consumed_at  TIMESTAMPTZ,
    attempts     INTEGER     NOT NULL DEFAULT 0,
    ip_address   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot path: "find latest unconsumed code for email+purpose" on every verify.
CREATE INDEX IF NOT EXISTS idx_auth_codes_email_purpose_created
    ON auth_codes (LOWER(email), purpose, created_at DESC);

-- Rate-limit / resend-cooldown lookups by email and by created_at window.
CREATE INDEX IF NOT EXISTS idx_auth_codes_created_at
    ON auth_codes (created_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
    id           UUID        PRIMARY KEY,
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT        UNIQUE NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    user_agent   TEXT,
    ip_address   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id
    ON sessions (user_id, created_at DESC);

-- Hot path: "is this token_hash a currently valid session?" on every request.
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash_active
    ON sessions (token_hash)
    WHERE revoked_at IS NULL;

COMMIT;
