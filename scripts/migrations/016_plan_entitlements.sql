-- Migration 016: per-plan subscription entitlements
--
-- Replaces "any payment -> users.plan_type = 'pro' (unlimited, all modes)"
-- with a concrete entitlement per purchase: which public plan, which mode
-- it covers, how many operations per period, the max source length, and
-- how many operations were already used. See scripts/entitlements.py.
--
-- No BEGIN/COMMIT: run_migrations.py runs each file in one implicit
-- transaction and re-runs every file on every deploy, so everything here
-- is idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
--
-- Existing users are NOT migrated onto a new plan: a legacy
-- plan_type='pro' row keeps working exactly as it was sold (unlimited
-- until plan_active_until), and nothing is inferred for it here -- there
-- is no way to know which new mode/plan a 500 ₽ Pro buyer would have
-- picked. Legacy pending pro_30d payments (plan_mode IS NULL) still
-- activate through the legacy branch of process_successful_payment().

-- 1. Terms snapshot on the payment row, captured at checkout from the
--    server catalog (same reasoning as amount/plan_type/duration_days in
--    migration 010). NULL = legacy payment created before this migration.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS plan_mode      TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS ops_limit      INTEGER;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS max_source_sec INTEGER;

-- Which payment provider created (and may confirm) the payment. Every row
-- before this migration came from Robokassa (now legacy). A provider's
-- webhook must refuse payments of any other provider, since invoice_id is
-- a shared order number (scripts/payment_providers.py).
ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'robokassa';

-- 2. One row per purchased period. A newer purchase for the same mode
--    closes the previous row (period_end = activation time) instead of
--    updating it, so a job's charged_subscription_id always points at the
--    exact period it was billed against.
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id              UUID        PRIMARY KEY,
    user_id         UUID        NOT NULL REFERENCES users(id),
    plan_id         TEXT        NOT NULL,
    plan_mode       TEXT        NOT NULL CHECK (plan_mode IN ('cut', 'trailer', 'streamer')),
    ops_limit       INTEGER     NOT NULL CHECK (ops_limit > 0),
    ops_used        INTEGER     NOT NULL DEFAULT 0 CHECK (ops_used >= 0),
    max_source_sec  INTEGER     NOT NULL CHECK (max_source_sec > 0),
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    payment_id      UUID        UNIQUE REFERENCES payments(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT user_subscriptions_ops_within_limit CHECK (ops_used <= ops_limit),
    CONSTRAINT user_subscriptions_period_order     CHECK (period_end >= period_start)
);

-- Hot path: "active subscriptions for this user" on every job creation
-- and every /api/auth/me.
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_mode_end
    ON user_subscriptions (user_id, plan_mode, period_end DESC);

DROP TRIGGER IF EXISTS trg_user_subscriptions_updated_at ON user_subscriptions;
CREATE TRIGGER trg_user_subscriptions_updated_at
    BEFORE UPDATE ON user_subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 3. Which subscription period a job was billed against (NULL = free
--    quota, legacy Pro, or a pre-016 job). Needed to audit usage and to
--    make a future "refund the operation of a failed job" exact.
ALTER TABLE generation_jobs
    ADD COLUMN IF NOT EXISTS charged_subscription_id UUID REFERENCES user_subscriptions(id);
