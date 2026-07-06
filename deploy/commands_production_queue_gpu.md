# Production Queue + Ephemeral GPU — Operations Commands

## Production Path

**VPS dispatcher → Vast direct image → worker_entrypoint → backend worker API → S3 → shutdown/destroy**

1. VPS dispatcher (`gpu_dispatcher.py`) picks up a queued job.
2. `gpu_orchestrator.py` (mode=`vast`) searches vast.ai for the cheapest matching GPU.
3. Creates a vast.ai instance with the **official Vast Create Instance API** payload:
   `runtype: "args"` (mapped from `VAST_LAUNCH_MODE=entrypoint`), image = `VAST_WORKER_IMAGE`.
   > **Note:** the official Vast API (`PUT /api/v0/asks/{id}/`) does **NOT** support
   > `runtype="entrypoint"` — valid values are `ssh`, `jupyter`, `args`, `ssh_proxy`,
   > `ssh_direct`, `jupyter_proxy`, `jupyter_direct`. `VAST_LAUNCH_MODE=entrypoint` is
   > only our own human-readable config name; internally it always maps to
   > `runtype: "args"`.
4. Env vars (secrets) passed via the Vast `env` field — a single Docker-flag
   **string** of plain `-e KEY=value` pairs only (e.g. `"-e KEY=value -e KEY2=value2"`),
   never a dict, and never embedded in logs. There is **no** `docker_options` field
   in the Vast API — that name is only used internally as a helper-function name,
   never sent as a payload field.
   > **2026-07 incident:** Vast rejected create-instance calls with
   > `{"success": false, "error": "invalid_args", "msg": "invalid env arguments"}`
   > when the `env` string included `--shm-size=8gb` and quoted `-e KEY="value"`
   > pairs. For the Create Instance API, `env` is now kept to **plain
   > `-e KEY=value` pairs only** — no `--shm-size`, no quoting. Keys with an
   > empty/falsy value are skipped entirely (never emit a bare `-e KEY=`).
5. `runtype: "args"` **preserves** the image's Docker `ENTRYPOINT` (`/entrypoint.sh`)
   and runs it with no extra args (`"args": []`). No SSH daemon, no openssh-server, no onstart.
6. `worker_entrypoint.sh` (image ENTRYPOINT) runs inside the container:
   - validates env vars, writes `.env.local`
   - `prod_preflight_check.py --role worker`
   - `model_downloader.py --mode $MODE` (downloads from S3)
   - `gpu_worker.py --once --job-id $JOB_ID` (api mode → backend worker API)
7. Results uploaded directly to S3.
8. Instance shuts down and is destroyed.

## GPU Provider

**Production GPU provider: [vast.ai](https://vast.ai)**

Vast.ai GPU instances are external and cannot reach the private PostgreSQL
server at `192.168.0.4`.  The worker uses `WORKER_BACKEND_MODE=api` —
all job operations go through `BACKEND_API_URL` worker endpoints.
No `DATABASE_URL` is passed to the GPU instance.

> **Vast launch mode:** Use `VAST_LAUNCH_MODE=entrypoint` (default) — maps to the
> official Vast api `runtype: "args"`, which **preserves** the image's Docker
> `ENTRYPOINT` and runs it directly with no extra args. **SSH mode and Jupyter mode
> override Docker `ENTRYPOINT`** (Vast installs `openssh-server`); do NOT use for
> automated workers. Use `VAST_LAUNCH_MODE=ssh_onstart` only for fallback/debug
> (api `runtype: "ssh"`; `onstart` calls `/entrypoint.sh`).
> There is **no `runtype="entrypoint"`** value in the official Vast API — only
> `ssh`, `jupyter`, `args`, `ssh_proxy`, `ssh_direct`, `jupyter_proxy`, `jupyter_direct`.

| Mode | GPU provider | Use case |
|---|---|---|
| `vast` | **vast.ai** — recommended production GPU | Direct image; WORKER_BACKEND_MODE=api |
| `timeweb` | Timeweb Cloud — optional/legacy | If already using Timeweb for GPU; can reach private DB |
| `webhook` | External orchestrator (n8n etc.) — optional | Visual workflow needed |
| `disabled` | None | Safe default |

---

## Docker Images — Build and Push (private repo flow)

The GitHub repo is **private**. vast.ai instances cannot git-clone it.
Build a Docker image with the code and push it to GHCR; instances pull the
image at runtime. Secrets are **never** baked into the image.

### Image tags

| Tag | Base | Use case |
|---|---|---|
| `sonya-worker:fast` | `python:3.11-slim-bookworm` + torch CUDA wheel | **Recommended for vast.ai production** — smallest cold-pull |
| `sonya-worker:latest` | `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime` | Stable/full fallback |

**Production recommendation:** Use `sonya-worker:fast` as `VAST_WORKER_IMAGE` on vast.ai.  
Cold-pull is significantly faster — no conda, no jupyter, no dev tools.  
Torch CUDA runtime is bundled inside the pip wheel; no nvidia/cuda base needed.

### Build the fast image (recommended for vast.ai)

```bash
# Linux / macOS (from repo root):
bash deploy/docker/build_worker_fast_image.sh
# Windows PowerShell:
.\deploy\docker\build_worker_fast_image.ps1
```

Script prints the uncompressed image size after build.

### Build the full (latest) image

```bash
bash deploy/docker/build_worker_image.sh
# Or on Windows:
.\deploy\docker\build_worker_image.ps1
```

### Manual push to GHCR

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u samnesvoj --password-stdin
docker push ghcr.io/samnesvoj/sonya-worker:fast
docker push ghcr.io/samnesvoj/sonya-worker:latest
```

### GitHub Actions (automatic build on push to main)

- `.github/workflows/build-worker-fast-image.yml` — builds and pushes `:fast`
- `.github/workflows/build-worker-image.yml`      — builds and pushes `:latest`

### Set GHCR_TOKEN on the VPS

```bash
# Add to /etc/sonya/env.local or systemd override:
GHCR_USERNAME=samnesvoj
GHCR_TOKEN=<github-pat-read-packages>   # never commit
```

---

## Quick sanity check — vast.ai dry-run (searches offers, no instance created)

Recommended first production test GPU: **RTX 3060 12 GB**.  
Avoid: Tesla V100, P100, K80, T4 (GPU exclude regex blocks them).  
Avoid: South Korea / KR, China / CN (location exclude regex blocks them — connectivity issues).  
Avoid: **unverified hosts** — they hang at "Loading" / "Verifying checksum" and never reach the backend API. Default `VAST_REQUIRE_VERIFIED=true` + `VAST_MIN_RELIABILITY=98` filters them out.  
Preferred locations: **US, EU (DE/NL/PL/FR/FI/SE), JP**.

```bash
GPU_ORCHESTRATOR_MODE=vast \
VAST_API_KEY=<your-key> \
VAST_DRY_RUN=true \
VAST_LAUNCH_MODE=entrypoint \
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04 \
VAST_WORKER_IMAGE=ghcr.io/samnesvoj/sonya-worker:fast \
VAST_GPU_MIN_VRAM=12 \
VAST_DISK_GB=50 \
VAST_GPU_INCLUDE_REGEX="RTX 3060|RTX 3070|RTX 3080|RTX 3090|RTX 4060|RTX 4070|RTX 4080|RTX 4090|A4000|A5000|L4|L40" \
VAST_GPU_EXCLUDE_REGEX="Tesla|V100|P100|K80|T4" \
VAST_LOCATION_EXCLUDE_REGEX="South Korea|Korea|KR|China|CN" \
VAST_REQUIRE_VERIFIED=true \
VAST_MIN_RELIABILITY=98 \
GHCR_USERNAME=samnesvoj \
GHCR_TOKEN=<token> \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=test \
AUTO_GPU_TRIGGER_ENABLED=true \
  python scripts/gpu_dispatcher.py --once
```

## Real vast.ai dispatch test (creates an instance, uses direct Docker image)

```bash
GPU_ORCHESTRATOR_MODE=vast \
VAST_API_KEY=<your-key> \
VAST_DRY_RUN=false \
VAST_LAUNCH_MODE=entrypoint \
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04 \
VAST_WORKER_IMAGE=ghcr.io/samnesvoj/sonya-worker:fast \
VAST_GPU_MIN_VRAM=12 \
VAST_DISK_GB=50 \
VAST_GPU_INCLUDE_REGEX="RTX 3060|RTX 3090|RTX 4090|A4000|A5000" \
VAST_GPU_EXCLUDE_REGEX="Tesla|V100|P100|K80|T4" \
VAST_LOCATION_EXCLUDE_REGEX="South Korea|Korea|KR|China|CN" \
GHCR_USERNAME=samnesvoj \
GHCR_TOKEN=<token> \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=<secret> \
S3_ENDPOINT_URL=<url> S3_ACCESS_KEY_ID=<id> S3_SECRET_ACCESS_KEY=<key> \
S3_BUCKET_NAME=sonya-prod S3_REGION=<region> MODELS_S3_BUCKET=<bucket> \
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

## Production startup SLA (Vast cold-start protection)

Vast.ai cold start (image pull + boot) can take **4+ minutes** on a slow
host. In production we cannot let one bad instance block the queue.

**Policy:**

- **240 sec max** (`VAST_STARTUP_TIMEOUT_SEC=240`) from `gpu_requested_at` to
  `worker_started_at`. If the worker has not called back via
  `/api/worker/status` within this window, the job is a **startup timeout**.
- **Auto destroy** the stale Vast instance (`destroy_vast_instance()` in
  `scripts/gpu_orchestrator.py`, `DELETE /instances/{contract_id}/`) — never
  let a stuck instance keep billing indefinitely. Destroy failures are logged
  as a warning but never block the job from being retried/failed.
- **Retry another host** — up to `VAST_MAX_STARTUP_RETRIES=3` attempts total.
  Each retry requeues the job as `status='queued'` so the next dispatcher
  pass picks a fresh offer; after the cap is reached the job is permanently
  `status='failed'` with `error='vast startup timeout after 240 sec'`.
- **Bad host cooldown** — the offending `host_id` / `machine_id` / `ip` is
  blacklisted for `VAST_SLOW_HOST_COOLDOWN_MIN=60` minutes in the
  `vast_bad_hosts` table (migration `007_vast_bad_hosts.sql`). Offers from a
  currently-blacklisted host are rejected during offer search with
  `reason=slow_startup_blacklist` (`scripts/gpu_orchestrator.py`,
  `_vast_search_offers`).
- **Warm worker/pool recommended** for real speed — cold start + retry is a
  *safety net*, not a performance fix. For latency-sensitive production
  traffic, keep a small pool of pre-warmed vast.ai instances (or a
  provider-side warm pool) instead of relying on cold dispatch alone.

**Env vars:**

```
VAST_STARTUP_TIMEOUT_SEC=240        # max sec from gpu_requested_at to worker_started_at
VAST_MAX_STARTUP_RETRIES=3          # max dispatch attempts before permanent fail
VAST_SLOW_HOST_COOLDOWN_MIN=60      # blacklist cooldown for a slow host/machine/ip
# Manual exclude lists (in addition to the automatic blacklist above):
VAST_EXCLUDE_HOST_IDS=              # comma-separated host_id values, e.g. "12345,67890"
VAST_EXCLUDE_MACHINE_IDS=           # comma-separated machine_id values
VAST_EXCLUDE_INSTANCE_IPS=          # comma-separated IPs
```

**How it works:**

1. `gpu_dispatcher.cleanup_stale_gpu_requests()` runs **before every dispatch
   pass** (both the continuous loop and `--once`). It queries
   `generation_jobs` for `status='gpu_requested'` rows where
   `gpu_requested_at < NOW() - 240s` and `worker_started_at IS NULL`.
2. For each stale job it reads `contract_id`, `offer_id`, `offer_gpu`,
   `host_id`, `machine_id`, `offer_ip` out of `orchestrator_payload`, calls
   `destroy_vast_instance(contract_id)`, and inserts a cooldown row into
   `vast_bad_hosts`.
3. `prod_job_store.mark_gpu_startup_timeout()` sets `error='vast startup
   timeout after 240 sec'` and either requeues (`status='queued'`,
   `orchestrator_error='startup timeout; retrying another offer'`) or
   permanently fails the job, depending on `attempts` vs
   `VAST_MAX_STARTUP_RETRIES`.
4. On the next dispatch pass, `_vast_search_offers()` filters out any offer
   matching a currently-blocked `host_id` / `machine_id` / `ip` — the retried
   job lands on a different host automatically.

### Database — Migration 007

```bash
# Apply (from repo root)
psql "$DATABASE_URL" -f scripts/migrations/007_vast_bad_hosts.sql

# Or via run_migrations.py (applies 001..007 in order)
python scripts/run_migrations.py
```

### Inspect the slow-host blacklist

```sql
-- Currently-blocked hosts
SELECT host_id, machine_id, ip, offer_id, reason, blocked_until
FROM vast_bad_hosts
WHERE blocked_until > now()
ORDER BY blocked_until DESC;

-- Jobs currently failing the startup SLA
SELECT id, mode, attempts, max_attempts, gpu_status, error, orchestrator_error
FROM generation_jobs
WHERE gpu_status IN ('startup_timeout_retry', 'startup_timeout_failed')
ORDER BY updated_at DESC;
```

---

## Debug Mode — diagnosing a Vast "Retrying in 1 second" loop

If the Vast instance card shows **"Retrying in 1 second"**, the container is
crashing immediately on start (before the log can normally be read, since
Vast restarts/destroys the container right away).

**`VAST_DEBUG_SLEEP_ON_FAIL=true`** — used ONLY for diagnosing this kind of
failure, never left on in normal production:

```bash
GPU_ORCHESTRATOR_MODE=vast \
VAST_API_KEY=<your-key> \
VAST_DRY_RUN=false \
VAST_LAUNCH_MODE=entrypoint \
VAST_WORKER_IMAGE=ghcr.io/samnesvoj/sonya-worker:fast \
VAST_DEBUG_SLEEP_ON_FAIL=true \
BACKEND_API_URL=https://sonya-e.com \
WORKER_SECRET=<secret> \
S3_ENDPOINT_URL=<url> S3_ACCESS_KEY_ID=<id> S3_SECRET_ACCESS_KEY=<key> \
S3_BUCKET_NAME=sonya-prod S3_REGION=<region> MODELS_S3_BUCKET=<bucket> \
AUTO_GPU_TRIGGER_ENABLED=true \
  python scripts/gpu_dispatcher.py --once
```

What happens:

1. `gpu_orchestrator.py` forwards `VAST_DEBUG_SLEEP_ON_FAIL=true` to the worker
   container via the Vast `env` field (a Docker-flag string, never logged).
2. `worker_entrypoint.sh` prints an early startup banner as the FIRST lines
   of the log (date, pwd, whoami, python version, env presence yes/no for
   `S3_BUCKET`, `S3_BUCKET_NAME`, `WORKER_SECRET` — never the raw values).
3. On any failure, the error trap prints `[ENTRYPOINT_ERROR] line=... exit_code=...`
   plus diagnostics, then **sleeps 900s** instead of exiting immediately —
   giving you time to open the Vast instance log/console before it retries
   or is destroyed.
4. A sanitized dump of the create payload is written to
   `/tmp/sonya_vast_last_payload.json` on the VPS (image, api_runtype, launch_mode,
   label, offer, env KEY NAMES, `env_has_shm_size` (always `false`), and
   `skipped_empty_env_keys` (names of optional vars omitted because they were
   empty) — secrets and the raw env string are never written).

**Turn `VAST_DEBUG_SLEEP_ON_FAIL` back OFF (default `false`) once the failure
has been diagnosed** — leaving it on in production wastes billable GPU time
on every failure.

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

> **Repo is private.** Set `VAST_WORKER_IMAGE` to your GHCR image so
> vast.ai instances pull the pre-built image instead of cloning the repo.

```
AUTO_GPU_TRIGGER_ENABLED=true
GPU_ORCHESTRATOR_MODE=vast
VAST_API_KEY=<your-vast-api-key>            # never commit
VAST_IMAGE=nvidia/cuda:12.2.0-devel-ubuntu22.04
VAST_WORKER_IMAGE=ghcr.io/samnesvoj/sonya-worker:fast     # pre-built image (private repo)
VAST_LAUNCH_MODE=entrypoint                 # entrypoint (default) | ssh_onstart
                                            # entrypoint = maps to official api runtype="args";
                                            #              Docker ENTRYPOINT is preserved and run as-is
                                            # SSH mode overrides ENTRYPOINT — use only ssh_onstart for debug
                                            # NOTE: runtype="entrypoint" does not exist in the Vast API
VAST_GPU_MIN_VRAM=12                        # 12 GB for RTX 3060; 24+ for heavier modes
VAST_DISK_GB=50
VAST_INSTANCE_LABEL_PREFIX=sonya-gpu
VAST_DRY_RUN=false
# GPU model filters:
VAST_GPU_INCLUDE_REGEX=RTX 3060|RTX 3070|RTX 3080|RTX 3090|RTX 4060|RTX 4070|RTX 4080|RTX 4090|A4000|A5000|L4|L40
VAST_GPU_EXCLUDE_REGEX=Tesla|V100|P100|K80|T4
# Location filters (avoid KR/CN — connectivity issues; prefer US/EU/JP):
VAST_LOCATION_EXCLUDE_REGEX=South Korea|Korea|KR|China|CN
# VAST_LOCATION_INCLUDE_REGEX=US|Germany|Netherlands|Poland|France|Finland|Sweden|Japan
# Host verification (unverified hosts hang at "Loading" and never reach backend API):
VAST_REQUIRE_VERIFIED=true
VAST_MIN_RELIABILITY=98
# GHCR credentials for pulling private image on vast.ai instance:
GHCR_USERNAME=samnesvoj
GHCR_TOKEN=<github-pat-read-packages>       # never commit
# Debug-safe mode (diagnose "Retrying in 1 second" loops only — turn off after):
VAST_DEBUG_SLEEP_ON_FAIL=false
# Production startup SLA (see "Production startup SLA" section above):
VAST_STARTUP_TIMEOUT_SEC=240
VAST_MAX_STARTUP_RETRIES=3
VAST_SLOW_HOST_COOLDOWN_MIN=60
# Manual offer exclusion (optional; in addition to the automatic blacklist):
# VAST_EXCLUDE_HOST_IDS=
# VAST_EXCLUDE_MACHINE_IDS=
# VAST_EXCLUDE_INSTANCE_IPS=
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
