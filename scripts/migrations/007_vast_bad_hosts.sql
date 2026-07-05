-- Migration 007: vast_bad_hosts — temporary blacklist/cooldown for slow-starting Vast hosts
--
-- Production startup SLA:
--   If a Vast worker does not report in (worker_started_at stays NULL) within
--   VAST_STARTUP_TIMEOUT_SEC (default 240s) after gpu_requested_at, the
--   offending instance is destroyed and its host/machine/ip is blocked here
--   for VAST_SLOW_HOST_COOLDOWN_MIN (default 60) minutes so the dispatcher
--   does not immediately retry the same slow offer.
--
-- All new objects use IF NOT EXISTS — safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS vast_bad_hosts (
    id            SERIAL       PRIMARY KEY,
    host_id       TEXT,
    machine_id    TEXT,
    ip            TEXT,
    offer_id      TEXT,
    reason        TEXT         NOT NULL DEFAULT 'slow_startup',
    blocked_until TIMESTAMPTZ  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Hot-path lookup: "is this host/machine/ip currently blocked?" (gpu_orchestrator.py
-- queries WHERE blocked_until > now() on every offer search).
CREATE INDEX IF NOT EXISTS ix_vast_bad_hosts_blocked_until
    ON vast_bad_hosts (blocked_until);

CREATE INDEX IF NOT EXISTS ix_vast_bad_hosts_host_id
    ON vast_bad_hosts (host_id)
    WHERE host_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_vast_bad_hosts_machine_id
    ON vast_bad_hosts (machine_id)
    WHERE machine_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_vast_bad_hosts_ip
    ON vast_bad_hosts (ip)
    WHERE ip IS NOT NULL;

COMMIT;
