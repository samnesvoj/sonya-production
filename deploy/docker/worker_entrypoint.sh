#!/usr/bin/env bash
# worker_entrypoint.sh — SONYA Docker GPU worker entrypoint
#
# Runs inside the container on a vast.ai ephemeral GPU instance.
# Secrets are injected via `docker run -e VAR=value` — never baked in.
# DATABASE_URL is NOT required (WORKER_BACKEND_MODE=api).
#
# Required env vars:
#   JOB_ID              UUID of the job to process
#   MODE                mode name (default: trailer_film_breaker)
#   BACKEND_API_URL     https://sonya-e.com
#   WORKER_SECRET       HMAC secret for worker API calls
#   S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
#   S3_BUCKET_NAME, S3_REGION, MODELS_S3_BUCKET

set -euo pipefail

LOG_DIR="/var/log/sonya"
LOG_FILE="${LOG_DIR}/gpu_worker_container.log"
mkdir -p "${LOG_DIR}"
exec > >(tee -a "${LOG_FILE}") 2>&1

ts()   { date '+%Y-%m-%dT%H:%M:%S%z'; }
log()  { echo "[$(ts)] [INFO]  $*"; }
warn() { echo "[$(ts)] [WARN]  $*"; }
fail() { echo "[$(ts)] [ERROR] $*"; exit 1; }

log "=== SONYA GPU worker container start ==="
log "JOB_ID=${JOB_ID:-<not set>}"
log "MODE=${MODE:-trailer_film_breaker}"
log "BACKEND_API_URL=${BACKEND_API_URL:-<not set>}"
log "WORKER_BACKEND_MODE=${WORKER_BACKEND_MODE:-api}"

# ── Required env validation ────────────────────────────────────────────────────
: "${JOB_ID:?JOB_ID env var is required}"
: "${BACKEND_API_URL:?BACKEND_API_URL env var is required}"
: "${WORKER_SECRET:?WORKER_SECRET env var is required}"
: "${S3_ENDPOINT_URL:?S3_ENDPOINT_URL env var is required}"
: "${S3_ACCESS_KEY_ID:?S3_ACCESS_KEY_ID env var is required}"
: "${S3_SECRET_ACCESS_KEY:?S3_SECRET_ACCESS_KEY env var is required}"
: "${S3_BUCKET_NAME:?S3_BUCKET_NAME env var is required}"
: "${S3_REGION:?S3_REGION env var is required}"
: "${MODELS_S3_BUCKET:?MODELS_S3_BUCKET env var is required}"

MODE="${MODE:-trailer_film_breaker}"
WORKER_BACKEND_MODE="${WORKER_BACKEND_MODE:-api}"
WORKDIR="/opt/sonya"

# ── Write .env.local (worker reads this for runtime config) ───────────────────
ENV_LOCAL="${WORKDIR}/.env.local"
log "Writing ${ENV_LOCAL}..."
{
    echo "WORKER_BACKEND_MODE=${WORKER_BACKEND_MODE}"
    echo "BACKEND_API_URL=${BACKEND_API_URL}"
    echo "S3_ENDPOINT_URL=${S3_ENDPOINT_URL}"
    echo "S3_ACCESS_KEY_ID=${S3_ACCESS_KEY_ID}"
    echo "S3_SECRET_ACCESS_KEY=${S3_SECRET_ACCESS_KEY}"
    echo "S3_BUCKET_NAME=${S3_BUCKET_NAME}"
    echo "S3_REGION=${S3_REGION}"
    echo "MODELS_S3_BUCKET=${MODELS_S3_BUCKET}"
    echo "WORKER_SECRET=${WORKER_SECRET}"
    echo "WORKER_ID=${WORKER_ID:-gpu-docker-${JOB_ID:0:8}}"
    echo "AUTO_GPU_TRIGGER_ENABLED=false"
    # Optional keys (injected only when set)
    [[ -n "${OPENROUTER_API_KEY:-}" ]]  && echo "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
    [[ -n "${GEMINI_API_KEY:-}" ]]       && echo "GEMINI_API_KEY=${GEMINI_API_KEY}"
    [[ -n "${ELEVENLABS_API_KEY:-}" ]]   && echo "ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY}"
    [[ -n "${ELEVENLABS_VOICE_ID:-}" ]]  && echo "ELEVENLABS_VOICE_ID=${ELEVENLABS_VOICE_ID}"
    # DATABASE_URL intentionally omitted — api mode does not need it
} > "${ENV_LOCAL}"
chmod 600 "${ENV_LOCAL}"
log ".env.local written (mode 600)."

# ── Pre-flight check ───────────────────────────────────────────────────────────
log "Running pre-flight check (worker role)..."
python "${WORKDIR}/scripts/prod_preflight_check.py" --role worker \
    || fail "Pre-flight check failed."
log "Pre-flight check passed."

# ── Model download ─────────────────────────────────────────────────────────────
log "Downloading models for MODE=${MODE}..."
python "${WORKDIR}/scripts/model_downloader.py" --mode "${MODE}" \
    || fail "Model download failed for MODE=${MODE}."
log "Models ready."

# ── Run worker (exactly once) ──────────────────────────────────────────────────
log "Starting gpu_worker.py --once --job-id ${JOB_ID}..."
python "${WORKDIR}/scripts/gpu_worker.py" \
    --once \
    --job-id "${JOB_ID}"

EXIT_CODE=$?
log "gpu_worker.py exited with code ${EXIT_CODE}."
exit "${EXIT_CODE}"
