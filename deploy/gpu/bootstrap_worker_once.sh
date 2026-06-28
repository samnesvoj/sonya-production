#!/usr/bin/env bash
# bootstrap_worker_once.sh — Ephemeral GPU instance bootstrap for SONYA
#
# Called once by a freshly-created GPU instance provisioned by the orchestrator.
# After the job completes the instance shuts itself down automatically.
#
# Required env vars injected by the orchestrator / cloud-init:
#   JOB_ID                  UUID of the job to process
#   MODE                    mode name, e.g. trailer_film_breaker
#   SHUTDOWN_AFTER_JOB      true | false  (default true)
#   DATABASE_URL            PostgreSQL DSN
#   S3_ENDPOINT_URL
#   S3_ACCESS_KEY_ID
#   S3_SECRET_ACCESS_KEY
#   S3_BUCKET_NAME
#   S3_REGION
#   BACKEND_API_URL         e.g. https://sonya-e.com
#   WORKER_SECRET           HMAC secret for worker API calls

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

# ── Validate required env ──────────────────────────────────────────────────────
: "${JOB_ID:?JOB_ID env var is required}"
: "${MODE:?MODE env var is required}"
: "${DATABASE_URL:?DATABASE_URL env var is required}"
: "${BACKEND_API_URL:?BACKEND_API_URL env var is required}"

SHUTDOWN_AFTER_JOB="${SHUTDOWN_AFTER_JOB:-true}"
REPO_URL="${REPO_URL:-https://github.com/samnesvoj/sonya-production.git}"
INSTALL_DIR="/opt/sonya"
VENV_DIR="${INSTALL_DIR}/.venv"
ENV_LOCAL="${INSTALL_DIR}/.env.local"

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

# ── Write .env.local from injected env vars ────────────────────────────────────
log "Writing ${ENV_LOCAL}..."
cat > "${ENV_LOCAL}" << EOF
DATABASE_URL=${DATABASE_URL}
S3_ENDPOINT_URL=${S3_ENDPOINT_URL:-}
S3_ACCESS_KEY_ID=${S3_ACCESS_KEY_ID:-}
S3_SECRET_ACCESS_KEY=${S3_SECRET_ACCESS_KEY:-}
S3_BUCKET_NAME=${S3_BUCKET_NAME:-}
S3_REGION=${S3_REGION:-}
BACKEND_API_URL=${BACKEND_API_URL}
WORKER_SECRET=${WORKER_SECRET:-}
WORKER_TOKEN=${WORKER_TOKEN:-}
WORKER_ID=${WORKER_ID:-gpu-ephemeral-${JOB_ID:0:8}}
AUTO_GPU_TRIGGER_ENABLED=false
EOF
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
