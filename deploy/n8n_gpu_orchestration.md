# GPU Orchestration — SONYA Ephemeral GPU Flow

## Overview

SONYA uses an **ephemeral GPU model**: a GPU instance is created for exactly
one job, processes it, uploads the result to S3, and then shuts itself down.
No persistent GPU workers exist on any server.

### Orchestration modes

| Mode | Description |
|---|---|
| `timeweb` | **Preferred / direct.** VPS calls Timeweb Cloud API directly — no n8n, no extra VPS. Budget-friendly. |
| `webhook` | Optional. VPS sends a signed HMAC webhook to n8n; n8n creates the GPU instance. Use only if you need a visual workflow editor and already run n8n. |
| `disabled` | Safe default — no GPU is ever created. |

> **n8n is optional.** If you do not have budget for a separate n8n VPS or
> cloud subscription, set `GPU_ORCHESTRATOR_MODE=timeweb` and the VPS will
> call Timeweb Cloud API directly. The `webhook` mode is kept for projects
> that already run n8n.

---

## Architecture — direct timeweb mode (recommended)

```
Client ──POST /api/generation/jobs──▶  VPS API (FastAPI)
                                            │  job created: status=queued, priority=N
                                            │  returns job_id immediately
VPS Dispatcher (systemd) ◀─poll─────────────┘
    │  every GPU_DISPATCH_INTERVAL_SECONDS (default 20 s)
    │  locks job, increments attempts
    ▼
gpu_orchestrator.trigger_gpu_for_job()  [GPU_ORCHESTRATOR_MODE=timeweb]
    │  POST https://api.timeweb.cloud/api/v1/servers
    │  Authorization: Bearer TIMEWEB_API_TOKEN
    │  user-data: cloud-init script with JOB_ID, MODE, env vars
    ▼
GPU Instance (ephemeral, Timeweb Cloud)
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

## Architecture — webhook mode (optional, requires n8n)

```
VPS Dispatcher
    │
    ▼
gpu_orchestrator.trigger_gpu_for_job()  [GPU_ORCHESTRATOR_MODE=webhook]
    │  POST signed webhook → n8n
    ▼
n8n Workflow
    │  verifies HMAC signature
    │  creates GPU instance via provider API (Hetzner, RunPod, Timeweb…)
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
| `gpu_orchestrator.py` | VPS (library) | Creates GPU via Timeweb API or webhook |
| `bootstrap_worker_once.sh` | GPU instance | Bootstraps environment, runs worker |
| `gpu_worker.py --once` | GPU instance | Processes one job, uploads, exits |

---

## Timeweb Mode — Env Vars (VPS .env.local)

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=timeweb
TIMEWEB_API_TOKEN=<bearer-token>          # never logged
TIMEWEB_GPU_PRESET_ID=<preset-id>
TIMEWEB_GPU_IMAGE_ID=<image-id>
TIMEWEB_GPU_REGION=<region-slug>
TIMEWEB_GPU_NAME_PREFIX=sonya-gpu
TIMEWEB_DELETE_AFTER_JOB=true
TIMEWEB_DRY_RUN=false                     # set true to test without creating server
TIMEWEB_SSH_KEY_ID=<optional>
TIMEWEB_NETWORK_ID=<optional>
TIMEWEB_PROJECT_ID=<optional>
GPU_BOOTSTRAP_SCRIPT_PATH=deploy/gpu/bootstrap_worker_once.sh
SHUTDOWN_AFTER_JOB=true
GPU_DISPATCH_INTERVAL_SECONDS=20
MAX_ACTIVE_GPU_JOBS=1
BACKEND_API_URL=https://sonya-e.com
DATABASE_URL=postgresql://...
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

## Environment Variables (GPU Instance via cloud-init / bootstrap)

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
MODELS_S3_BUCKET=...
BACKEND_API_URL=https://sonya-e.com
WORKER_SECRET=<hmac-secret>
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
```

In `timeweb` mode these are embedded in the cloud-init `user_data` script
sent to the Timeweb API.  They are **never written to logs**.

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

- `TIMEWEB_API_TOKEN` and `GPU_ORCHESTRATOR_WEBHOOK_SECRET` are **never logged**
- All secrets forwarded to the GPU instance are embedded in cloud-init `user_data` —
  they are not written to any log file by the orchestrator
- `.env.local` on the GPU instance is written with `chmod 600`
- Worker API uses HMAC-signed requests (`WORKER_SECRET`)
- Dispatcher enforces `MAX_ACTIVE_GPU_JOBS` concurrency cap
