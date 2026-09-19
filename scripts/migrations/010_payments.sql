-- Migration 010: Robokassa payments (one-time subscription purchase)
--
-- One-time payment flow only -- no recurring columns here by design (see
-- docs/SONYA_AUDIT.md / plan discussion: Robokassa recurring has no test
-- mode and requires separate approval; it is a future migration).
--
-- No BEGIN/COMMIT -- run_migrations.py already executes each file's full
-- text inside a single implicit transaction, and every file in
-- scripts/migrations/ is re-executed on every deploy (no migration-tracking
-- table), so this must stay idempotent (IF NOT EXISTS everywhere).

CREATE SEQUENCE IF NOT EXISTS robokassa_invoice_id_seq;

CREATE TABLE IF NOT EXISTS payments (
    id                    UUID        PRIMARY KEY,
    invoice_id            BIGINT      NOT NULL UNIQUE DEFAULT nextval('robokassa_invoice_id_seq'),
    user_id               UUID        NOT NULL REFERENCES users(id),
    plan_id               TEXT        NOT NULL,
    amount                NUMERIC(10,2) NOT NULL,
    currency              TEXT        NOT NULL DEFAULT 'RUB',
    -- Snapshot of PLAN_CATALOG[plan_id] at checkout time, same reasoning as
    -- `amount` below: process_successful_payment() must activate exactly
    -- what the user was shown and paid for, even if PLAN_CATALOG changes
    -- (e.g. Pro duration edited) before the webhook confirms this specific
    -- payment. Never re-looked-up from the live catalog for an existing row.
    plan_type             TEXT        NOT NULL,
    duration_days         INTEGER     NOT NULL,
    -- Captured at payment-creation time, not re-read from the current
    -- ROBOKASSA_TEST_MODE env value when a callback later arrives --
    -- otherwise flipping the flag mid-flight would break signature
    -- verification for payments already in flight.
    is_test               BOOLEAN     NOT NULL DEFAULT FALSE,
    status                TEXT        NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')),
    result_received_at    TIMESTAMPTZ,
    paid_at               TIMESTAMPTZ,
    raw_result_params     JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments (user_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_payments_updated_at ON payments;
CREATE TRIGGER trg_payments_updated_at
    BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
