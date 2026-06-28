# GPU Orchestration — SONYA Ephemeral GPU Flow

## Overview

SONYA uses an **ephemeral GPU model**: a GPU instance is created for exactly
one job, processes it, uploads the result to S3, and then shuts itself down.
No persistent GPU workers exist on any server.

### Production GPU provider: vast.ai

**vast.ai is the recommended production GPU provider.**

Vast.ai GPU instances are external to the Timeweb VPS and cannot reach the
private PostgreSQL server (`192.168.0.4`).  The GPU worker therefore uses
`WORKER_BACKEND_MODE=api` — it never connects to the database directly.
All job operations (claim, status update, complete, fail, file registration)
go through `BACKEND_API_URL` worker HTTP endpoints authenticated with
`WORKER_SECRET`.  S3 operations remain direct.

### Timeweb Cloud

Timeweb is used for: **VPS, PostgreSQL, S3 storage, domain**.
Timeweb GPU (`GPU_ORCHESTRATOR_MODE=timeweb`) is **optional/legacy** — it is
not the primary GPU path and should only be used if vast.ai is unavailable.

### n8n

n8n is **optional**.  It is not required for production.  If you need a visual
workflow editor and already run n8n, you can use `GPU_ORCHESTRATOR_MODE=webhook`.
Otherwise use `GPU_ORCHESTRATOR_MODE=vast` and the VPS calls vast.ai directly.

### Orchestration modes

| Mode | GPU provider | DB access on GPU | Notes |
|---|---|---|---|
| `vast` | **vast.ai** — recommended | API only (no DATABASE_URL) | Production path |
| `timeweb` | Timeweb Cloud GPU | Direct PostgreSQL possible | Optional/legacy |
| `webhook` | External (n8n → any) | Depends on n8n workflow | Optional, needs n8n |
| `disabled` | None | — | Safe default |

---

## Architecture — vast.ai mode (production / recommended)

```
Client ──POST /api/generation/jobs──▶  VPS API (FastAPI, Timeweb VPS)
                                            │  job created: status=queued
                                            │  returns job_id immediately
VPS Dispatcher (systemd) ◀─poll─────────────┘
    │  every GPU_DISPATCH_INTERVAL_SECONDS
    │  locks job, increments attempts
    ▼
gpu_orchestrator.trigger_gpu_for_job()  [GPU_ORCHESTRATOR_MODE=vast]
    │  GET  https://console.vast.ai/api/v0/bundles/ — find cheapest GPU offer
    │  PUT  https://console.vast.ai/api/v0/asks/{id}/ — create instance
    │  startup script (onstart): env vars + git clone + bootstrap
    │  WORKER_BACKEND_MODE=api injected — no DATABASE_URL passed
    ▼
GPU Instance (ephemeral, vast.ai)
    │  runs deploy/gpu/bootstrap_worker_once.sh
    │  apt-get git python3-venv ffmpeg …
    │  git clone repo → /opt/sonya
    │  pip install requirements-worker.txt
    │  python scripts/model_downloader.py --mode $MODE
    │  python scripts/gpu_worker.py --once --job-id $JOB_ID
    │       └─ uses WORKER_BACKEND_MODE=api:
    │           POST /api/worker/claim          (claim job)
    │           POST /api/worker/jobs/{id}/status  (status updates)
    │           POST /api/worker/jobs/{id}/files   (register output files)
    │           POST /api/worker/jobs/{id}/complete (mark done)
    │  uploads results directly to S3
    │  shutdown -h now
```

---

## Architecture — webhook mode (optional, requires n8n)

```
VPS Dispatcher
    │
    ▼
gpu_orchestrator.trigger_gpu_for_job()  [GPU_ORCHESTRATOR_MODE=webhook]
    │  POST signed HMAC webhook → n8n
    ▼
n8n Workflow
    │  verifies HMAC signature
    │  creates GPU instance via provider API (Hetzner, RunPod, vast.ai…)
    │  passes JOB_ID, MODE, env vars via cloud-init / user-data
    ▼
GPU Instance (ephemeral)
    │  … same bootstrap as above …
```

---

## Components

| Component | Location | Role |
|---|---|---|
| `prod_generation_api.py` | VPS | Accepts uploads, creates queued jobs |
| `gpu_dispatcher.py` | VPS (systemd) | Polls DB, calls orchestrator per job |
| `gpu_orchestrator.py` | VPS (library) | Creates GPU via vast.ai / Timeweb / webhook |
| `bootstrap_worker_once.sh` | GPU instance | Bootstraps environment, runs worker |
| `gpu_worker.py --once` | GPU instance | Processes one job, uploads, exits |

---

## vast.ai Mode — Env Vars (VPS .env.local)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=vast
VAST_API_KEY=<bearer-token>               # never logged
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04
VAST_GPU_MIN_VRAM=24
VAST_DISK_GB=50
VAST_INSTANCE_LABEL_PREFIX=sonya-gpu
VAST_DRY_RUN=false                        # set true for testing without creating instance
VAST_GPU_NAME=                            # optional, e.g. RTX4090
SHUTDOWN_AFTER_JOB=true
GPU_DISPATCH_INTERVAL_SECONDS=20
MAX_ACTIVE_GPU_JOBS=1
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...             # VPS-only — NOT forwarded to GPU instance

# Forwarded to GPU instance via startup script (no DATABASE_URL):
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

## Webhook Mode — Payload (VPS → n8n)

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

## Webhook Mode — Signature Verification in n8n

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

## Job Lifecycle

```
queued
  └─ dispatcher locks job, calls orchestrator
       ├─ success → gpu_requested (gpu_status=requested)
       └─ failure → queued (retry) or failed (attempts >= max_attempts)

gpu_requested
  └─ GPU instance booting → gpu_booting
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

- `VAST_API_KEY`, `TIMEWEB_API_TOKEN`, `GPU_ORCHESTRATOR_WEBHOOK_SECRET` **never logged**
- Secrets forwarded to GPU instance are embedded in the startup script —
  not written to any log file by the orchestrator
- `DATABASE_URL` is **not forwarded** to vast.ai GPU instances
- `.env.local` on GPU instance is written with `chmod 600`
- Worker API uses HMAC-signed requests (`WORKER_SECRET`)
- Dispatcher enforces `MAX_ACTIVE_GPU_JOBS` concurrency cap
