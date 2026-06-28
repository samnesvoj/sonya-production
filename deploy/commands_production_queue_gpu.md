# Production Queue + Ephemeral GPU — Operations Commands

## GPU Provider

**Production GPU provider: [vast.ai](https://vast.ai)**

Vast.ai GPU instances are external and cannot reach the private PostgreSQL
server at `192.168.0.4`.  The worker therefore uses `WORKER_BACKEND_MODE=api`
and communicates with the VPS exclusively through `BACKEND_API_URL` worker
endpoints.  No `DATABASE_URL` is passed to the GPU instance.

| Mode | GPU provider | Use case |
|---|---|---|
| `vast` | **vast.ai** — recommended production GPU | External GPU; uses WORKER_BACKEND_MODE=api |
| `timeweb` | Timeweb Cloud — optional/legacy | If already using Timeweb for GPU; can reach private DB |
| `webhook` | External orchestrator (n8n etc.) — optional | Visual workflow needed |
| `disabled` | None | Safe default |

---

## Quick sanity check — vast.ai dry-run (no instance created)

```bash
GPU_ORCHESTRATOR_MODE=vast \
VAST_API_KEY=test \
VAST_DRY_RUN=true \
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04 \
VAST_GPU_MIN_VRAM=24 \
VAST_DISK_GB=50 \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=test \
AUTO_GPU_TRIGGER_ENABLED=true \
  python scripts/gpu_dispatcher.py --once
```

## Real vast.ai dispatch test (creates an instance)

```bash
GPU_ORCHESTRATOR_MODE=vast \
VAST_API_KEY=<your-key> \
VAST_DRY_RUN=false \
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04 \
VAST_GPU_MIN_VRAM=24 \
VAST_DISK_GB=50 \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=<secret> \
S3_ENDPOINT_URL=<url> S3_ACCESS_KEY_ID=<id> S3_SECRET_ACCESS_KEY=<key> \
S3_BUCKET_NAME=sonya-prod S3_REGION=<region> \
AUTO_GPU_TRIGGER_ENABLED=true \
  python scripts/gpu_dispatcher.py --once
```

---

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

# One-shot dispatch (dry-run — no GPU triggered)
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
# API mode (vast.ai — no DATABASE_URL)
WORKER_BACKEND_MODE=api \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=<secret> \
  python scripts/gpu_worker.py --once --job-id <uuid>

# DB mode (internal VPS — has DATABASE_URL)
WORKER_BACKEND_MODE=db \
  python scripts/gpu_worker.py --once --job-id <uuid>
```

---

## Bootstrap — Manual Test on GPU Instance

```bash
# API mode (external GPU — vast.ai)
JOB_ID=<uuid> MODE=trailer_film_breaker \
WORKER_BACKEND_MODE=api \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=<secret> \
SHUTDOWN_AFTER_JOB=false \
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

## Dispatcher Env Vars — vast mode / production (VPS .env.local)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=vast
VAST_API_KEY=<your-vast-api-key>            # never commit
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04
VAST_GPU_MIN_VRAM=24
VAST_DISK_GB=50
VAST_INSTANCE_LABEL_PREFIX=sonya-gpu
VAST_DRY_RUN=false
VAST_GPU_NAME=                              # optional GPU model filter
SHUTDOWN_AFTER_JOB=true
GPU_DISPATCH_INTERVAL_SECONDS=20
MAX_ACTIVE_GPU_JOBS=1
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...               # VPS only — NOT sent to vast.ai GPU

# Forwarded to the GPU instance (no DATABASE_URL — vast.ai uses API mode):
WORKER_SECRET=<hmac-secret>
S3_ENDPOINT_URL=...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=sonya-prod
S3_REGION=...
MODELS_S3_BUCKET=...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
```

---

## Dispatcher Env Vars — timeweb mode (optional/legacy)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=timeweb
TIMEWEB_API_TOKEN=<token>
TIMEWEB_GPU_PRESET_ID=<preset-id>
TIMEWEB_GPU_IMAGE_ID=<image-id>
TIMEWEB_GPU_REGION=<region-slug>
TIMEWEB_GPU_NAME_PREFIX=sonya-gpu
TIMEWEB_DELETE_AFTER_JOB=true
TIMEWEB_DRY_RUN=false
GPU_BOOTSTRAP_SCRIPT_PATH=deploy/gpu/bootstrap_worker_once.sh
SHUTDOWN_AFTER_JOB=true
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...
```

---

## Dispatcher Env Vars — webhook mode (optional, requires n8n)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=webhook
GPU_ORCHESTRATOR_WEBHOOK_URL=https://n8n.sonya-e.com/webhook/gpu-trigger
GPU_ORCHESTRATOR_WEBHOOK_SECRET=<secret>
GPU_INSTANCE_TYPE=A100
GPU_IMAGE=ubuntu-22.04-cuda-12-2
GPU_REGION=eu-central-1
SHUTDOWN_AFTER_JOB=true
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...
```
