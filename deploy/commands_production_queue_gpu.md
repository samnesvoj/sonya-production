# Production Queue + Ephemeral GPU — Operations Commands

## VPS — Dispatcher Service

```bash
# Install / reload systemd unit
sudo cp deploy/systemd/sonya-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sonya-dispatcher
sudo systemctl start sonya-dispatcher

# Status and live logs
sudo systemctl status sonya-dispatcher
sudo journalctl -u sonya-dispatcher -f

# Restart after config change
sudo systemctl restart sonya-dispatcher

# One-shot dispatch (dry-run or cron)
AUTO_GPU_TRIGGER_ENABLED=false python scripts/gpu_dispatcher.py --once

# One-shot dispatch (live)
AUTO_GPU_TRIGGER_ENABLED=true python scripts/gpu_dispatcher.py --once
```

---

## Database — Migration 006

```bash
# Apply (from repo root)
psql "$DATABASE_URL" -f scripts/migrations/006_gpu_queue_priority.sql

# Or via run_migrations.py
python scripts/run_migrations.py
```

---

## Database — Queue Inspection

```sql
-- Next job the dispatcher would pick
SELECT id, mode, status, priority, attempts, max_attempts,
       locked_until, queued_at, gpu_status
FROM generation_jobs
WHERE status = 'queued'
  AND attempts < max_attempts
  AND (locked_until IS NULL OR locked_until < now())
ORDER BY priority DESC, queued_at ASC
LIMIT 10;

-- Active GPU jobs
SELECT id, mode, status, gpu_status,
       gpu_requested_at, worker_started_at
FROM generation_jobs
WHERE status IN ('gpu_requested','gpu_booting','worker_started','model_downloading');

-- Failed jobs last 24 h
SELECT id, mode, attempts, max_attempts, last_error, failed_at
FROM generation_jobs
WHERE status = 'failed'
  AND failed_at > now() - INTERVAL '24 hours'
ORDER BY failed_at DESC;

-- Requeue failed job manually
UPDATE generation_jobs
SET status       = 'queued',
    attempts     = 0,
    locked_until = NULL,
    gpu_status   = NULL,
    last_error   = NULL,
    updated_at   = now()
WHERE id = '<uuid>';

-- Override job priority (admin)
UPDATE generation_jobs SET priority = 1000 WHERE id = '<uuid>';
```

---

## GPU Worker — Manual Run (on GPU instance)

```bash
# Process one specific job and exit
python scripts/gpu_worker.py --once --job-id <uuid>

# Claim next queued job and exit
python scripts/gpu_worker.py --once
```

---

## Bootstrap — Manual Test on GPU Instance

```bash
# Export required env vars first, then:
JOB_ID=<uuid> MODE=trailer_film_breaker SHUTDOWN_AFTER_JOB=false \
  bash deploy/gpu/bootstrap_worker_once.sh

# Logs:
tail -f /var/log/sonya/gpu_worker_bootstrap.log
```

---

## Validation

```bash
# From repo root:
python scripts/validate_repo_integrity.py
python scripts/prod_preflight_check.py --role backend
python scripts/prod_preflight_check.py --role worker
```

---

## Monitoring

```bash
# Count active GPU jobs
psql "$DATABASE_URL" -c "
  SELECT COUNT(*) FROM generation_jobs
  WHERE status IN ('gpu_requested','gpu_booting','worker_started','model_downloading');
"

# Jobs queued > 10 minutes (possible stuck)
psql "$DATABASE_URL" -c "
  SELECT id, mode, priority, attempts, queued_at
  FROM generation_jobs
  WHERE status = 'queued'
    AND queued_at < now() - INTERVAL '10 minutes'
  ORDER BY queued_at;
"
```

---

## Dispatcher Env Vars (VPS .env.local)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=webhook
GPU_ORCHESTRATOR_WEBHOOK_URL=https://n8n.sonya-e.com/webhook/gpu-trigger
GPU_ORCHESTRATOR_WEBHOOK_SECRET=<secret>
GPU_INSTANCE_TYPE=A100
GPU_IMAGE=ubuntu-22.04-cuda-12-2
GPU_REGION=eu-central-1
SHUTDOWN_AFTER_JOB=true
GPU_DISPATCH_INTERVAL_SECONDS=20
MAX_ACTIVE_GPU_JOBS=1
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...
```
