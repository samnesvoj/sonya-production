# n8n GPU Orchestration — SONYA Ephemeral GPU Flow

## Overview

SONYA uses an **ephemeral GPU model**: a GPU instance is created for exactly
one job, processes it, uploads the result to S3, and then shuts itself down.
No persistent GPU workers exist on any server.

```
Client ──POST /api/generation/jobs──▶  VPS API (FastAPI)
                                            │  job created: status=queued, priority=N
                                            │  returns job_id immediately
VPS Dispatcher (systemd) ◀─poll─────────────┘
    │  every GPU_DISPATCH_INTERVAL_SECONDS (default 20 s)
    │  locks job, increments attempts
    ▼
gpu_orchestrator.trigger_gpu_for_job()
    │  POST signed webhook → n8n
    ▼
n8n Workflow
    │  verifies HMAC signature
    │  creates GPU instance via provider API (Hetzner, RunPod, Lambda…)
    │  passes JOB_ID, MODE, env vars via cloud-init / user-data
    ▼
GPU Instance (ephemeral)
    │  runs deploy/gpu/bootstrap_worker_once.sh
    │  apt-get git python3-venv ffmpeg …
    │  git clone repo → /opt/sonya
    │  pip install requirements-worker.txt
    │  python scripts/prod_preflight_check.py --role worker
    │  python scripts/model_downloader.py --mode $MODE
    │  python scripts/gpu_worker.py --once --job-id $JOB_ID
    │  uploads result to S3
    │  calls /api/worker/jobs/{job_id}/complete
    │  shutdown -h now
```

---

## Components

| Component | Location | Role |
|---|---|---|
| `prod_generation_api.py` | VPS | Accepts uploads, creates queued jobs |
| `gpu_dispatcher.py` | VPS (systemd) | Polls DB, calls orchestrator per job |
| `gpu_orchestrator.py` | VPS (library) | Sends signed webhook to n8n |
| `bootstrap_worker_once.sh` | GPU instance | Bootstraps environment, runs worker |
| `gpu_worker.py --once` | GPU instance | Processes one job, uploads, exits |

---

## Webhook Payload (VPS → n8n)

```json
{
  "job_id": "uuid",
  "mode": "trailer_film_breaker",
  "priority": 500,
  "plan": "pro",
  "backend_api_url": "https://sonya-e.com",
  "gpu_instance_type": "A100",
  "gpu_image": "ubuntu-22.04-cuda-12-2",
  "gpu_region": "eu-central-1",
  "shutdown_after_job": true
}
```

Signed with `X-Orchestrator-Signature: HMAC-SHA256(body, GPU_ORCHESTRATOR_WEBHOOK_SECRET)`.

---

## Signature Verification in n8n

```javascript
const crypto = require('crypto');
const secret = $env.GPU_ORCHESTRATOR_WEBHOOK_SECRET;
const body   = JSON.stringify($input.body);
const expected = crypto
  .createHmac('sha256', secret)
  .update(Buffer.from(body))
  .digest('hex');
const received = $input.headers['x-orchestrator-signature'];
if (!received || received !== expected) {
  throw new Error('Invalid webhook signature');
}
```

---

## n8n Workflow Steps

1. **Webhook Trigger** — POST `/webhook/gpu-trigger`
2. **Verify Signature** — Function node (above)
3. **Create GPU Instance** — HTTP Request → provider API
   - Inject `JOB_ID`, `MODE`, all env vars via cloud-init user-data
   - Reference `bootstrap_worker_once.sh` from the repo
4. **Update Status** (optional) — POST `{backend_api_url}/api/worker/jobs/{job_id}/status`
   body `{"status": "gpu_booting"}`

---

## Environment Variables (VPS .env.local)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=webhook
GPU_ORCHESTRATOR_WEBHOOK_URL=https://n8n.sonya-e.com/webhook/gpu-trigger
GPU_ORCHESTRATOR_WEBHOOK_SECRET=<long-random>
GPU_INSTANCE_TYPE=A100
GPU_IMAGE=ubuntu-22.04-cuda-12-2
GPU_REGION=eu-central-1
SHUTDOWN_AFTER_JOB=true
GPU_DISPATCH_INTERVAL_SECONDS=20
MAX_ACTIVE_GPU_JOBS=1
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...
```

---

## Environment Variables (GPU Instance via bootstrap)

```
JOB_ID=<uuid>
MODE=trailer_film_breaker
SHUTDOWN_AFTER_JOB=true
DATABASE_URL=postgresql://...
S3_ENDPOINT_URL=...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=sonya-prod
S3_REGION=...
BACKEND_API_URL=https://sonya-e.com
WORKER_SECRET=<hmac-secret>
```

---

## Job Lifecycle

```
queued
  └─ dispatcher locks job, calls orchestrator
       ├─ success → gpu_requested (gpu_status=requested)
       └─ failure → queued (retry) or failed (attempts >= max_attempts)

gpu_requested
  └─ n8n creates instance → gpu_booting
       └─ worker starts → worker_started
            └─ downloading → model_downloading → mode_running
                 → analyzing → yolo → scripting → tts → subtitles
                 → assembling → uploading_result → completed
```

---

## Priority Table

| Plan header (`X-User-Plan`) | Priority |
|---|---|
| admin | 1000 |
| pro | 500 |
| paid | 300 |
| free | 100 |
| unknown / missing | 100 |

Priority is **always server-assigned**. Clients cannot override it.

---

## Retry Logic

- `max_attempts = 3` per job (migration 006 column)
- On orchestrator failure: `attempts += 1`, `locked_until = NULL`
- If `attempts >= max_attempts` → job → `failed`
- Lock timeout: `locked_until` expires after 120 s; dispatcher retries automatically
- `FOR UPDATE SKIP LOCKED` prevents two dispatcher instances racing

---

## Security Notes

- `GPU_ORCHESTRATOR_WEBHOOK_SECRET` is **never logged**
- `.env.local` on GPU instance is written with `chmod 600`
- Worker API uses HMAC-signed requests (`WORKER_SECRET`)
- Dispatcher enforces `MAX_ACTIVE_GPU_JOBS` concurrency cap
