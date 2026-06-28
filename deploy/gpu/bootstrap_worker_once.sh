#!/usr/bin/env bash
# bootstrap_worker_once.sh — Ephemeral GPU instance bootstrap for SONYA
#
# Called once by a freshly-created GPU instance provisioned by the orchestrator.
# After the job completes the instance shuts itself down automatically.
#
# Backend modes (WORKER_BACKEND_MODE):
#   db   — default; direct PostgreSQL (requires DATABASE_URL)
#   api  — HTTP-only via BACKEND_API_URL (no DATABASE_URL needed)
#          Used with external GPU providers (vast.ai) that cannot reach
#          private PostgreSQL at 192.168.0.4.
#
# Required env vars — common:
#   JOB_ID                  UUID of the job to process
#   MODE                    mode name, e.g. trailer_film_breaker
#   SHUTDOWN_AFTER_JOB      true | false  (default true)
#   BACKEND_API_URL         https://sonya-e.com
#   S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME
#
# Required env vars — api mode (WORKER_BACKEND_MODE=api):
#   WORKER_SECRET           HMAC secret for worker API calls
#   (DATABASE_URL is NOT required)
#
# Required env vars — db mode (WORKER_BACKEND_MODE=db):
#   DATABASE_URL            PostgreSQL DSN

set -euo pipefail

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_DIR="/var/log/sonya"
LOG_FILE="${LOG_DIR}/gpu_worker_bootstrap.log"
mkdir -p "${LOG_DIR}"
exec > >(tee -a "${LOG_FILE}") 2>&1

ts()   { date '+%Y-%m-%dT%H:%M:%S%z'; }
log()  { echo "[$(ts)] [INFO]  $*"; }
warn() { echo "[$(ts)] [WARN]  $*"; }
fail() { echo "[$(ts)] [ERROR] $*"; exit 1; }

log "=== SONYA GPU worker bootstrap start ==="
log "JOB_ID=${JOB_ID:-<not set>}"
log "MODE=${MODE:-<not set>}"
log "WORKER_BACKEND_MODE=${WORKER_BACKEND_MODE:-db}"
log "BACKEND_API_URL=${BACKEND_API_URL:-<not set>}"

# ── Validate required env ──────────────────────────────────────────────────────
: "${JOB_ID:?JOB_ID env var is required}"
: "${MODE:?MODE env var is required}"
: "${BACKEND_API_URL:?BACKEND_API_URL env var is required}"

WORKER_BACKEND_MODE="${WORKER_BACKEND_MODE:-db}"
SHUTDOWN_AFTER_JOB="${SHUTDOWN_AFTER_JOB:-true}"
REPO_URL="${REPO_URL:-https://github.com/samnesvoj/sonya-production.git}"
INSTALL_DIR="/opt/sonya"
VENV_DIR="${INSTALL_DIR}/.venv"
ENV_LOCAL="${INSTALL_DIR}/.env.local"

if [[ "${WORKER_BACKEND_MODE}" == "api" ]]; then
    # API mode: no DATABASE_URL needed; require WORKER_SECRET for auth
    : "${WORKER_SECRET:?WORKER_SECRET is required when WORKER_BACKEND_MODE=api}"
    log "Backend mode: api (HTTP worker endpoints, no direct DB access)"
else
    # DB mode: require DATABASE_URL for direct PostgreSQL access
    : "${DATABASE_URL:?DATABASE_URL is required when WORKER_BACKEND_MODE=db}"
    log "Backend mode: db (direct PostgreSQL)"
fi

# ── System dependencies ────────────────────────────────────────────────────────
log "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git \
    python3-venv \
    python3-pip \
    ffmpeg \
    curl \
    unzip

log "System dependencies installed."

# ── Clone / update repo ────────────────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Repo exists — pulling latest..."
    git -C "${INSTALL_DIR}" fetch --quiet origin
    git -C "${INSTALL_DIR}" reset --hard origin/main
else
    log "Cloning repo to ${INSTALL_DIR}..."
    git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
fi
log "Repo ready."

# ── Python virtual environment ─────────────────────────────────────────────────
if [[ ! -d "${VENV_DIR}" ]]; then
    log "Creating Python venv..."
    python3 -m venv "${VENV_DIR}"
fi

log "Installing Python dependencies from requirements-worker.txt..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements-worker.txt"
log "Python dependencies installed."

# ── Write .env.local ──────────────────────────────────────────────────────────
log "Writing ${ENV_LOCAL}..."
{
    echo "WORKER_BACKEND_MODE=${WORKER_BACKEND_MODE}"
    echo "BACKEND_API_URL=${BACKEND_API_URL}"
    echo "S3_ENDPOINT_URL=${S3_ENDPOINT_URL:-}"
    echo "S3_ACCESS_KEY_ID=${S3_ACCESS_KEY_ID:-}"
    echo "S3_SECRET_ACCESS_KEY=${S3_SECRET_ACCESS_KEY:-}"
    echo "S3_BUCKET_NAME=${S3_BUCKET_NAME:-}"
    echo "S3_REGION=${S3_REGION:-}"
    echo "MODELS_S3_BUCKET=${MODELS_S3_BUCKET:-}"
    echo "WORKER_SECRET=${WORKER_SECRET:-}"
    echo "WORKER_ID=${WORKER_ID:-gpu-ephemeral-${JOB_ID:0:8}}"
    echo "AUTO_GPU_TRIGGER_ENABLED=false"
    # DATABASE_URL only written when in db mode and the var is set
    if [[ "${WORKER_BACKEND_MODE}" != "api" && -n "${DATABASE_URL:-}" ]]; then
        echo "DATABASE_URL=${DATABASE_URL}"
    fi
} > "${ENV_LOCAL}"
chmod 600 "${ENV_LOCAL}"
log ".env.local written (mode 600)."

# Load vars into current shell
set -a
# shellcheck source=/dev/null
source "${ENV_LOCAL}"
set +a

# ── Pre-flight check ───────────────────────────────────────────────────────────
log "Running pre-flight check (worker role)..."
"${VENV_DIR}/bin/python" "${INSTALL_DIR}/scripts/prod_preflight_check.py" --role worker \
    || fail "Pre-flight check failed. Aborting."
log "Pre-flight check passed."

# ── Model download ─────────────────────────────────────────────────────────────
log "Downloading models for MODE=${MODE}..."
"${VENV_DIR}/bin/python" "${INSTALL_DIR}/scripts/model_downloader.py" --mode "${MODE}" \
    || fail "Model download failed for MODE=${MODE}."
log "Models ready."

# ── Process the job (exactly once) ────────────────────────────────────────────
log "Starting gpu_worker.py --once --job-id ${JOB_ID}..."
"${VENV_DIR}/bin/python" "${INSTALL_DIR}/scripts/gpu_worker.py" \
    --once \
    --job-id "${JOB_ID}"

EXIT_CODE=$?
log "gpu_worker.py exited with code ${EXIT_CODE}."

# ── Shutdown ───────────────────────────────────────────────────────────────────
if [[ "${SHUTDOWN_AFTER_JOB}" == "true" ]]; then
    log "SHUTDOWN_AFTER_JOB=true — shutting down instance now."
    shutdown -h now
else
    warn "SHUTDOWN_AFTER_JOB=${SHUTDOWN_AFTER_JOB} — instance will NOT shut down automatically."
fi

exit "${EXIT_CODE}"
