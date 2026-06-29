"""
gpu_orchestrator.py
===================
Ephemeral GPU provisioning — multi-mode.

GPU_ORCHESTRATOR_MODE selects which backend creates the GPU instance:

  disabled  — nothing is created (safe default)
  webhook   — sends a signed HMAC webhook to an external orchestrator (n8n etc.)
  timeweb   — directly calls Timeweb Cloud API; no n8n / external service needed
              (legacy/optional — Timeweb is used for VPS/DB/S3, not GPU)
  vast      — directly calls vast.ai API; preferred production GPU provider
              GPU instance uses WORKER_BACKEND_MODE=api (no DATABASE_URL needed)

This module is NOT a worker and does NOT generate video.
It only requests creation of a temporary GPU instance for a single job.
The instance runs deploy/gpu/bootstrap_worker_once.sh, processes the job,
uploads the result to S3, then shuts itself down.

────────────────────────────────────────────────────────────────────────────────
Common env vars
────────────────────────────────────────────────────────────────────────────────
  GPU_ORCHESTRATOR_MODE       disabled|webhook|timeweb|vast   (default disabled)
  SHUTDOWN_AFTER_JOB          true | false                    (default true)
  BACKEND_API_URL             https://sonya-e.com

────────────────────────────────────────────────────────────────────────────────
webhook mode env vars
────────────────────────────────────────────────────────────────────────────────
  GPU_ORCHESTRATOR_WEBHOOK_URL    https://n8n.sonya-e.com/webhook/gpu-trigger
  GPU_ORCHESTRATOR_WEBHOOK_SECRET HMAC signing secret  (never logged)
  GPU_INSTANCE_TYPE               e.g. A100
  GPU_IMAGE                       e.g. ubuntu-22.04-cuda-12-2
  GPU_REGION                      e.g. eu-central-1

────────────────────────────────────────────────────────────────────────────────
timeweb mode env vars  (optional/legacy)
────────────────────────────────────────────────────────────────────────────────
  TIMEWEB_API_TOKEN           Timeweb Cloud API bearer token        (never logged)
  TIMEWEB_PROJECT_ID          optional project ID
  TIMEWEB_GPU_PRESET_ID       hardware preset (GPU configuration)
  TIMEWEB_GPU_IMAGE_ID        OS image ID
  TIMEWEB_GPU_REGION          region/location slug
  TIMEWEB_SSH_KEY_ID          optional SSH key ID for emergency access
  TIMEWEB_NETWORK_ID          optional private network ID
  TIMEWEB_GPU_NAME_PREFIX     name prefix for created instances (default sonya-gpu)
  TIMEWEB_DELETE_AFTER_JOB    delete instance when job completes  (default true)
  TIMEWEB_DRY_RUN             true → log sanitized payload, skip API call
  GPU_BOOTSTRAP_SCRIPT_PATH   path inside repo to bootstrap script

────────────────────────────────────────────────────────────────────────────────
vast mode env vars  (production GPU provider)
────────────────────────────────────────────────────────────────────────────────
  VAST_API_KEY                vast.ai API key                       (never logged)
  VAST_IMAGE                  Docker image (e.g. nvidia/cuda:12.2.0-devel-ubuntu22.04)
  VAST_GPU_NAME               optional legacy exact-match filter (prefer regex vars below)
  VAST_GPU_MIN_VRAM           minimum VRAM in GB (default 24)
  VAST_DISK_GB                disk size in GB (default 50)
  VAST_INSTANCE_LABEL_PREFIX  label prefix for created instances (default sonya-gpu)
  VAST_DRY_RUN                true → search offers, log chosen offer, skip instance creation
  VAST_GPU_INCLUDE_REGEX      case-insensitive regex; only offers matching this are accepted
                              default: RTX 3060|RTX 3070|...|RTX 4090|A4000|A5000|L4|L40
  VAST_GPU_EXCLUDE_REGEX      case-insensitive regex; offers matching this are rejected
                              default: Tesla|V100|P100|K80|T4
                              Set to empty string to disable the respective filter.

  # Docker image mode (recommended for private repos):
  VAST_WORKER_IMAGE           pre-built Docker image to run on the instance
                              e.g. ghcr.io/samnesvoj/sonya-worker:latest
                              When set, the startup script pulls & runs this image
                              instead of git-cloning the repo. Repo stays private.
  GHCR_USERNAME               GitHub username for ghcr.io login (optional)
  GHCR_TOKEN                  GitHub PAT with read:packages scope  (never logged)

  # Forwarded to the GPU instance via startup script (never logged):
  BACKEND_API_URL     WORKER_SECRET
  S3_ENDPOINT_URL     S3_ACCESS_KEY_ID    S3_SECRET_ACCESS_KEY
  S3_BUCKET_NAME      S3_REGION           MODELS_S3_BUCKET
  OPENROUTER_API_KEY  GEMINI_API_KEY      ELEVENLABS_API_KEY  ELEVENLABS_VOICE_ID
  SHUTDOWN_AFTER_JOB
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Common config ──────────────────────────────────────────────────────────────

_MODE           = os.environ.get("GPU_ORCHESTRATOR_MODE", "disabled").lower()
SHUTDOWN_AFTER_JOB: bool = os.environ.get("SHUTDOWN_AFTER_JOB", "true").lower() == "true"
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "https://sonya-e.com")

AUTO_GPU_TRIGGER_ENABLED: bool = (
    os.environ.get("AUTO_GPU_TRIGGER_ENABLED", "false").lower() == "true"
)

# ── Webhook mode config ────────────────────────────────────────────────────────

_WEBHOOK_URL    = os.environ.get("GPU_ORCHESTRATOR_WEBHOOK_URL", "")
_WEBHOOK_SECRET = os.environ.get("GPU_ORCHESTRATOR_WEBHOOK_SECRET", "")
GPU_INSTANCE_TYPE = os.environ.get("GPU_INSTANCE_TYPE", "")
GPU_IMAGE         = os.environ.get("GPU_IMAGE", "")
GPU_REGION        = os.environ.get("GPU_REGION", "")

# ── Timeweb mode config (optional/legacy) ─────────────────────────────────────

_TW_API_BASE      = "https://api.timeweb.cloud/api/v1"
_TW_API_TOKEN     = os.environ.get("TIMEWEB_API_TOKEN", "")        # never logged
_TW_PROJECT_ID    = os.environ.get("TIMEWEB_PROJECT_ID", "")
_TW_PRESET_ID     = os.environ.get("TIMEWEB_GPU_PRESET_ID", "")
_TW_IMAGE_ID      = os.environ.get("TIMEWEB_GPU_IMAGE_ID", "")
_TW_REGION        = os.environ.get("TIMEWEB_GPU_REGION", "")
_TW_SSH_KEY_ID    = os.environ.get("TIMEWEB_SSH_KEY_ID", "")
_TW_NETWORK_ID    = os.environ.get("TIMEWEB_NETWORK_ID", "")
_TW_NAME_PREFIX   = os.environ.get("TIMEWEB_GPU_NAME_PREFIX", "sonya-gpu")
_TW_DELETE_AFTER  = os.environ.get("TIMEWEB_DELETE_AFTER_JOB", "true").lower() == "true"
_TW_DRY_RUN       = os.environ.get("TIMEWEB_DRY_RUN", "false").lower() == "true"
_BOOTSTRAP_PATH   = os.environ.get(
    "GPU_BOOTSTRAP_SCRIPT_PATH", "deploy/gpu/bootstrap_worker_once.sh"
)
_REPO_URL = os.environ.get(
    "REPO_URL", "https://github.com/samnesvoj/sonya-production.git"
)

# ── Vast mode config (production GPU provider) ────────────────────────────────

_VAST_API_BASE          = "https://console.vast.ai/api/v0"
_VAST_API_KEY           = os.environ.get("VAST_API_KEY", "")       # never logged
_VAST_IMAGE             = os.environ.get("VAST_IMAGE", "nvidia/cuda:12.2.0-devel-ubuntu22.04")
_VAST_GPU_NAME          = os.environ.get("VAST_GPU_NAME", "")      # optional legacy exact-match filter
_VAST_GPU_MIN_VRAM      = int(os.environ.get("VAST_GPU_MIN_VRAM", "24"))
_VAST_DISK_GB           = int(os.environ.get("VAST_DISK_GB", "50"))
_VAST_LABEL_PREFIX      = os.environ.get("VAST_INSTANCE_LABEL_PREFIX", "sonya-gpu")
_VAST_DRY_RUN           = os.environ.get("VAST_DRY_RUN", "false").lower() == "true"

# Docker image mode: when set, the instance pulls and runs the pre-built image
# instead of git-cloning the private repo. Required for private repositories.
_VAST_WORKER_IMAGE      = os.environ.get("VAST_WORKER_IMAGE", "")  # e.g. ghcr.io/samnesvoj/sonya-worker:latest
_GHCR_USERNAME          = os.environ.get("GHCR_USERNAME", "")
_GHCR_TOKEN             = os.environ.get("GHCR_TOKEN", "")         # never logged

# GPU model allow/deny lists (case-insensitive regex matched against gpu_name field).
# Defaults select consumer RTX and professional Ada/Ampere cards and explicitly
# exclude legacy data-center GPUs that are slow and cheap but unsuitable for
# real-time video inference (Tesla V100/P100/K80/T4).
_VAST_GPU_INCLUDE_REGEX: str = os.environ.get(
    "VAST_GPU_INCLUDE_REGEX",
    r"RTX 3060|RTX 3070|RTX 3080|RTX 3090|RTX 4060|RTX 4070|RTX 4080|RTX 4090|A4000|A5000|L4|L40",
)
_VAST_GPU_EXCLUDE_REGEX: str = os.environ.get(
    "VAST_GPU_EXCLUDE_REGEX",
    r"Tesla|V100|P100|K80|T4\b",
)

# Compiled once at import time (empty pattern → None = no constraint)
_vast_include_re: Optional[re.Pattern[str]] = (
    re.compile(_VAST_GPU_INCLUDE_REGEX, re.IGNORECASE) if _VAST_GPU_INCLUDE_REGEX else None
)
_vast_exclude_re: Optional[re.Pattern[str]] = (
    re.compile(_VAST_GPU_EXCLUDE_REGEX, re.IGNORECASE) if _VAST_GPU_EXCLUDE_REGEX else None
)

# Location allow/deny lists.
# Matched case-insensitively against country, country_code, geolocation, region, city
# fields in the offer.  Default excludes South Korea and China which have shown
# connectivity/latency problems with sonya-e.com backend.
# Recommended locations for first production test: US, EU (DE/NL/FR/FI/SE/PL), JP.
_VAST_LOCATION_INCLUDE_REGEX: str = os.environ.get("VAST_LOCATION_INCLUDE_REGEX", "")
_VAST_LOCATION_EXCLUDE_REGEX: str = os.environ.get(
    "VAST_LOCATION_EXCLUDE_REGEX",
    r"South Korea|Korea|^KR$|\bKR\b|China|^CN$|\bCN\b",
)

_vast_location_include_re: Optional[re.Pattern[str]] = (
    re.compile(_VAST_LOCATION_INCLUDE_REGEX, re.IGNORECASE)
    if _VAST_LOCATION_INCLUDE_REGEX else None
)
_vast_location_exclude_re: Optional[re.Pattern[str]] = (
    re.compile(_VAST_LOCATION_EXCLUDE_REGEX, re.IGNORECASE)
    if _VAST_LOCATION_EXCLUDE_REGEX else None
)

# Host verification and reliability requirements.
# Unverified hosts often hang at "Verifying checksum" / "Loading" and never
# reach the backend API.  Require verified=true and reliability >= 98 by default.
_VAST_REQUIRE_VERIFIED: bool = (
    os.environ.get("VAST_REQUIRE_VERIFIED", "true").lower() != "false"
)
_VAST_MIN_RELIABILITY: float = float(os.environ.get("VAST_MIN_RELIABILITY", "98"))

# Env vars forwarded to Timeweb GPU instances via cloud-init (may contain secrets)
_TW_WORKER_ENV_VARS: List[str] = [
    "BACKEND_API_URL",
    "DATABASE_URL",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET_NAME",
    "S3_REGION",
    "MODELS_S3_BUCKET",
    "WORKER_SECRET",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "SHUTDOWN_AFTER_JOB",
]

# Env vars forwarded to vast.ai GPU instances via startup script.
# No DATABASE_URL — vast.ai GPU uses WORKER_BACKEND_MODE=api.
_VAST_WORKER_ENV_VARS: List[str] = [
    "BACKEND_API_URL",
    "WORKER_SECRET",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET_NAME",
    "S3_REGION",
    "MODELS_S3_BUCKET",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "SHUTDOWN_AFTER_JOB",
]

# Secret env vars — values are masked in all logs and dry-run output
_SECRET_ENV_VARS = frozenset({
    "DATABASE_URL",
    "S3_SECRET_ACCESS_KEY",
    "WORKER_SECRET",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
    "GHCR_TOKEN",
})


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _sign_payload(body: bytes) -> str:
    """HMAC-SHA256 over the raw JSON body. Secret is never logged."""
    if not _WEBHOOK_SECRET:
        logger.warning("GPU_ORCHESTRATOR_WEBHOOK_SECRET not set — webhook unsigned")
        return ""
    return hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _sanitized_env_map(job_id: str, mode: str, var_list: List[str]) -> Dict[str, str]:
    """Return {VAR: value_or_masked} for logging / dry-run. Secrets are replaced with ***."""
    result: Dict[str, str] = {"JOB_ID": job_id, "MODE": mode}
    for var in var_list:
        val = os.environ.get(var, "")
        if not val:
            continue
        result[var] = "***" if var in _SECRET_ENV_VARS else val
    return result


def _sanitize_api_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any token/secret fields that might appear in API responses."""
    _sensitive = {"password", "token", "secret", "key", "access"}
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if any(s in k.lower() for s in _sensitive):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _sanitize_api_response(v)
        else:
            out[k] = v
    return out


def _build_env_export_lines(
    job_id: str,
    mode: str,
    extra_exports: Optional[Dict[str, str]],
    var_list: List[str],
) -> List[str]:
    """
    Build 'export VAR=value' shell lines for a startup script.
    Secrets are embedded directly (no logging happens here).
    """
    lines = [
        f'export JOB_ID="{job_id}"',
        f'export MODE="{mode}"',
    ]
    if extra_exports:
        for k, v in extra_exports.items():
            safe = v.replace("'", "'\\''")
            lines.append(f"export {k}='{safe}'")
    for var in var_list:
        val = os.environ.get(var, "")
        if val:
            safe = val.replace("'", "'\\''")
            lines.append(f"export {var}='{safe}'")
    return lines


# ── Webhook mode ───────────────────────────────────────────────────────────────

def _trigger_webhook(
    job_id: str,
    mode: str,
    priority: int,
    plan: Optional[str],
    extra: Optional[Dict[str, Any]],
) -> Tuple[bool, Dict[str, Any]]:
    if not _WEBHOOK_URL:
        logger.warning(
            "GPU_ORCHESTRATOR_WEBHOOK_URL not set — cannot trigger GPU job_id=%s", job_id
        )
        return False, {"error": "GPU_ORCHESTRATOR_WEBHOOK_URL not configured"}

    payload: Dict[str, Any] = {
        "job_id": job_id,
        "mode": mode,
        "priority": priority,
        "plan": plan,
        "backend_api_url": BACKEND_API_URL,
        "gpu_instance_type": GPU_INSTANCE_TYPE,
        "gpu_image": GPU_IMAGE,
        "gpu_region": GPU_REGION,
        "shutdown_after_job": SHUTDOWN_AFTER_JOB,
    }
    if extra:
        payload.update(extra)

    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = _sign_payload(body)
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if signature:
        headers["X-Orchestrator-Signature"] = signature

    try:
        import requests  # type: ignore

        resp = requests.post(_WEBHOOK_URL, data=body, headers=headers, timeout=15)
        if resp.ok:
            logger.info(
                "gpu_webhook_ok job_id=%s mode=%s http=%d", job_id, mode, resp.status_code
            )
            return True, payload
        logger.warning(
            "gpu_webhook_failed job_id=%s mode=%s http=%d", job_id, mode, resp.status_code
        )
        return False, {"error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.warning("gpu_webhook_error job_id=%s exc=%s", job_id, exc)
        return False, {"error": str(exc)}


# ── Timeweb mode (optional/legacy) ────────────────────────────────────────────

def _build_timeweb_cloud_init(job_id: str, mode: str) -> str:
    """Build cloud-init user-data shell script for a Timeweb GPU instance."""
    env_lines = _build_env_export_lines(job_id, mode, None, _TW_WORKER_ENV_VARS)
    script_lines = [
        "#!/usr/bin/env bash",
        "# SONYA Timeweb ephemeral GPU cloud-init — generated by gpu_orchestrator.py",
        f"# job_id={job_id}  mode={mode}",
        "set -euo pipefail",
        "",
        "# ── Environment ──────────────────────────────────────────────────────",
    ] + env_lines + [
        "",
        "# ── Bootstrap ────────────────────────────────────────────────────────",
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -qq",
        "apt-get install -y -qq git",
        "",
        'INSTALL_DIR="/opt/sonya"',
        f'REPO_URL="{_REPO_URL}"',
        "",
        'if [[ -d "${INSTALL_DIR}/.git" ]]; then',
        '    git -C "${INSTALL_DIR}" fetch --quiet origin',
        '    git -C "${INSTALL_DIR}" reset --hard origin/main',
        "else",
        '    git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"',
        "fi",
        "",
        f'bash "${{INSTALL_DIR}}/{_BOOTSTRAP_PATH}"',
    ]
    return "\n".join(script_lines) + "\n"


def _trigger_timeweb(job_id: str, mode: str) -> Tuple[bool, Dict[str, Any]]:
    """Create an ephemeral GPU server on Timeweb Cloud for a single job."""
    job_short = job_id[:8]
    instance_name = f"{_TW_NAME_PREFIX}-{job_short}"

    missing = []
    if not _TW_API_TOKEN:
        missing.append("TIMEWEB_API_TOKEN")
    if not _TW_PRESET_ID:
        missing.append("TIMEWEB_GPU_PRESET_ID")
    if not _TW_IMAGE_ID:
        missing.append("TIMEWEB_GPU_IMAGE_ID")
    if missing:
        err = f"timeweb mode missing required env vars: {', '.join(missing)}"
        logger.error("timeweb_config_error job_id=%s %s", job_id, err)
        return False, {"error": err}

    user_data = _build_timeweb_cloud_init(job_id, mode)
    user_data_b64 = base64.b64encode(user_data.encode()).decode()

    server_payload: Dict[str, Any] = {
        "name": instance_name,
        "preset_id": int(_TW_PRESET_ID) if _TW_PRESET_ID.isdigit() else _TW_PRESET_ID,
        "os_id": int(_TW_IMAGE_ID) if _TW_IMAGE_ID.isdigit() else _TW_IMAGE_ID,
        "user_data": user_data_b64,
    }
    if _TW_REGION:
        server_payload["location"] = _TW_REGION
    if _TW_PROJECT_ID:
        server_payload["project_id"] = _TW_PROJECT_ID
    if _TW_SSH_KEY_ID:
        server_payload["ssh_keys_ids"] = [
            int(_TW_SSH_KEY_ID) if _TW_SSH_KEY_ID.isdigit() else _TW_SSH_KEY_ID
        ]
    if _TW_NETWORK_ID:
        server_payload["networks"] = [{"id": _TW_NETWORK_ID, "type": "private"}]

    sanitized_config = {
        "provider": "timeweb",
        "instance_name": instance_name,
        "preset_id": _TW_PRESET_ID,
        "image_id": _TW_IMAGE_ID,
        "region": _TW_REGION or "(not set)",
        "delete_after_job": _TW_DELETE_AFTER,
        "env_forwarded": _sanitized_env_map(job_id, mode, _TW_WORKER_ENV_VARS),
        "user_data_bytes": len(user_data),
    }

    if _TW_DRY_RUN:
        logger.info(
            "timeweb_dry_run job_id=%s instance=%s config=%s",
            job_id, instance_name, json.dumps(sanitized_config, indent=2),
        )
        return True, {
            "ok": True, "provider": "timeweb", "dry_run": True,
            "instance_name": instance_name, "sanitized_config": sanitized_config,
        }

    logger.info(
        "timeweb_create_server job_id=%s instance=%s preset=%s image=%s region=%s",
        job_id, instance_name, _TW_PRESET_ID, _TW_IMAGE_ID, _TW_REGION,
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_TW_API_TOKEN}",
    }
    try:
        import requests  # type: ignore

        resp = requests.post(
            f"{_TW_API_BASE}/servers", json=server_payload, headers=headers, timeout=30
        )
        try:
            raw_body = resp.json()
        except Exception:
            raw_body = {"raw_text": resp.text[:500]}

        sanitized_body = _sanitize_api_response(raw_body) if isinstance(raw_body, dict) else raw_body

        if resp.ok:
            server_data = raw_body.get("server", raw_body)
            instance_id = str(server_data.get("id", ""))
            logger.info(
                "timeweb_server_created job_id=%s instance_id=%s name=%s",
                job_id, instance_id, instance_name,
            )
            return True, {
                "ok": True, "provider": "timeweb",
                "instance_id": instance_id, "instance_name": instance_name,
                "status": server_data.get("status", ""),
                "raw_response": sanitized_body,
            }

        logger.warning(
            "timeweb_api_error job_id=%s http=%d body=%s",
            job_id, resp.status_code, json.dumps(sanitized_body)[:300],
        )
        return False, {
            "ok": False, "provider": "timeweb",
            "error": f"Timeweb API HTTP {resp.status_code}",
            "raw_response": sanitized_body,
        }
    except Exception as exc:
        logger.warning("timeweb_exception job_id=%s exc=%s", job_id, exc)
        return False, {"ok": False, "provider": "timeweb", "error": str(exc)}


# ── Vast mode (production GPU provider) ───────────────────────────────────────
#
# Two deployment paths:
#
#   Direct image (VAST_WORKER_IMAGE set — recommended, production):
#     • Vast pulls the pre-built image directly.
#     • Env vars are passed via the `env` dict field in the create payload
#       (sent over HTTPS to vast.ai API, never embedded in a logged script).
#     • `onstart` contains only a minimal one-liner ("bash /entrypoint.sh").
#     • NO git clone, NO docker pull/run, NO Docker-in-Docker inside onstart.
#     • worker_entrypoint.sh (image ENTRYPOINT) handles the full job flow.
#
#   Git-clone fallback (VAST_WORKER_IMAGE not set — public repos only):
#     • Vast boots with a base CUDA/PyTorch image.
#     • `onstart` contains a full setup script (base64-encoded to avoid
#       exec-shebang-as-path errors).
#     • Clones the repo, installs deps, calls bootstrap_worker_once.sh.


def _build_vast_env_dict(job_id: str, mode: str) -> Dict[str, str]:
    """
    Build the env vars dict for direct image mode.

    Passed to Vast.ai as the ``env`` field in the create payload.
    Vast injects these as Docker ``-e`` flags when it starts the container.

    Values are NEVER written to logs — only the key names appear in
    sanitized_config / log output.

    S3 bucket aliasing
    ------------------
    S3_BUCKET_NAME (used by orchestrator) and S3_BUCKET (expected by preflight
    and some S3 tools) are kept in sync: both are set to the same value so that
    prod_preflight_check.py passes regardless of which field it inspects.
    """
    env: Dict[str, str] = {
        "JOB_ID": job_id,
        "MODE": mode,
        "WORKER_BACKEND_MODE": "api",
        "SHUTDOWN_AFTER_JOB": os.environ.get("SHUTDOWN_AFTER_JOB", "true"),
    }
    for var in _VAST_WORKER_ENV_VARS:
        val = os.environ.get(var, "")
        if val:
            env[var] = val

    # Ensure both S3_BUCKET_NAME and S3_BUCKET are present (alias pair).
    # prod_preflight_check.py accepts either; some S3 libs expect S3_BUCKET.
    bucket = env.get("S3_BUCKET_NAME") or os.environ.get("S3_BUCKET", "")
    if bucket:
        env.setdefault("S3_BUCKET_NAME", bucket)
        env["S3_BUCKET"] = bucket           # always inject alias

    return env


def _sanitized_startup_preview(job_id: str, mode: str) -> Dict[str, Any]:
    """Log-safe summary — no secret values included."""
    env_present = [v for v in _VAST_WORKER_ENV_VARS if os.environ.get(v)]
    env_missing  = [v for v in _VAST_WORKER_ENV_VARS if not os.environ.get(v)]
    return {
        "job_id": job_id,
        "mode": mode,
        "worker_backend_mode": "api",
        "deployment_mode": "direct-image" if _VAST_WORKER_IMAGE else "git-clone",
        "effective_image": _VAST_WORKER_IMAGE or _VAST_IMAGE,
        "env_vars_present": env_present,
        "env_vars_missing": env_missing,
    }


# ── Git-clone fallback helpers (public repo only) ─────────────────────────────

def _wrap_vast_startup_command(script: str) -> str:
    """
    Wrap a multiline bash startup script for safe git-clone fallback execution.

    Without this wrapper the shebang line gets exec-ed as a file path:
      exec: "#!/usr/bin/env bash\\n...": stat ...: no such file or directory

    Encodes the script as base64 and produces a one-liner:
      bash -lc "$(echo BASE64 | base64 -d)"

    Used ONLY for git-clone fallback mode (VAST_WORKER_IMAGE not set).
    In direct image mode the onstart is simply "bash /entrypoint.sh".
    """
    encoded = base64.b64encode(script.encode()).decode()
    return f'bash -lc "$(echo {encoded} | base64 -d)"'


def _build_vast_startup_script(job_id: str, mode: str) -> str:
    """
    Build the git-clone fallback startup script for a bare CUDA instance.

    Used ONLY when VAST_WORKER_IMAGE is not set (public repos, dev/testing).
    The script is base64-encoded via _wrap_vast_startup_command before being
    passed to vast.ai so Docker never tries to exec the shebang as a file path.
    Secrets are embedded (set +x prevents bash -x tracing them).
    """
    env_lines = _build_env_export_lines(
        job_id, mode,
        extra_exports={"WORKER_BACKEND_MODE": "api"},
        var_list=_VAST_WORKER_ENV_VARS,
    )

    body = [
        "#!/usr/bin/env bash",
        "# SONYA vast.ai git-clone startup — generated by gpu_orchestrator.py",
        f"# job_id={job_id}  mode={mode}",
        "set -euo pipefail",
        "set +x",   # suppress bash -x so secret values are not traced
        "",
        'LOG_DIR="/var/log/sonya"',
        'mkdir -p "$LOG_DIR"',
        'exec > >(tee -a "$LOG_DIR/vast_startup.log") 2>&1',
        "",
        "# ── Environment ──────────────────────────────────────────────────────",
    ] + env_lines + [
        "",
        "# ── System setup ─────────────────────────────────────────────────────",
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -qq 2>/dev/null || true",
        "apt-get install -y -qq git python3-venv python3-pip ffmpeg curl 2>/dev/null || true",
        "",
        "# ── Clone repo ───────────────────────────────────────────────────────",
        'INSTALL_DIR="/opt/sonya"',
        f'REPO_URL="{_REPO_URL}"',
        "",
        'if [[ -d "$INSTALL_DIR/.git" ]]; then',
        '    git -C "$INSTALL_DIR" fetch --quiet origin',
        '    git -C "$INSTALL_DIR" reset --hard origin/main',
        "else",
        '    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"',
        "fi",
        "",
        "# ── Bootstrap ────────────────────────────────────────────────────────",
        'bash "$INSTALL_DIR/deploy/gpu/bootstrap_worker_once.sh"',
    ]

    return "\n".join(body) + "\n"


def _get_offer_gpu_name(offer: Dict[str, Any]) -> str:
    """
    Extract a GPU model name from an offer dict.
    vast.ai uses different field names across API versions.
    """
    for field in ("gpu_name", "gpu_names", "gpu", "model", "gpu_display_name"):
        val = offer.get(field)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list) and val:
            return str(val[0])
    return ""


def _get_offer_location_label(offer: Dict[str, Any]) -> str:
    """
    Build a composite location string from whatever geographic fields vast.ai
    returns.  The label is matched against VAST_LOCATION_INCLUDE/EXCLUDE_REGEX.

    vast.ai API may return location info in various fields depending on API
    version and offer type; we try all known fields and concatenate non-empty
    values so a single regex can match country name, ISO code, city, or region.
    """
    parts: List[str] = []
    for field in (
        "country",          # "United States", "South Korea" …
        "country_code",     # "US", "KR", "DE" …
        "geolocation",      # "Seoul, KR" or "Frankfurt, DE" …
        "location",         # free-form location string
        "region",           # "EU", "NA", "ASIA" …
        "city",             # "Seoul", "Amsterdam" …
        "datacenter",       # sometimes contains city/country
        "host_region",      # some API versions
    ):
        val = offer.get(field)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())

    # Also check nested dicts (e.g. {"location": {"country": "KR", "city": "Seoul"}})
    for field in ("location", "geolocation"):
        nested = offer.get(field)
        if isinstance(nested, dict):
            for sub in ("country", "country_code", "city", "region"):
                sv = nested.get(sub)
                if isinstance(sv, str) and sv.strip():
                    parts.append(sv.strip())

    return " | ".join(dict.fromkeys(parts))  # deduplicate while preserving order


def _get_offer_reliability(offer: Dict[str, Any]) -> Optional[float]:
    """
    Return the best reliability score (0–100) found in the offer, or None.

    vast.ai exposes reliability under different field names across API versions
    and offer types.  Higher is better; 100 = perfect.
    """
    for field in (
        "reliability2",     # newer API: float 0.0–1.0 (multiply by 100)
        "reliability",      # older API: sometimes 0–100, sometimes 0.0–1.0
        "host_reliability",
        "reliability_mult",
    ):
        val = offer.get(field)
        if isinstance(val, (int, float)) and val is not None:
            # Normalise: values ≤ 1.0 are fractions → convert to percentage
            return float(val * 100) if val <= 1.0 else float(val)
    return None


def _check_offer_verified(offer: Dict[str, Any]) -> Optional[bool]:
    """
    Return True if the host is verified, False if explicitly unverified, None if unknown.

    Checks multiple field names and string sentinel values used across vast.ai API versions.
    """
    for field in ("verified", "is_verified", "host_verified"):
        val = offer.get(field)
        if val is None:
            continue
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            lower = val.lower()
            if lower in ("true", "yes", "verified", "1"):
                return True
            if lower in ("false", "no", "unverified", "0", "pending", ""):
                return False
        if isinstance(val, (int, float)):
            return bool(val)
    # "verification" field: may be "verified" / "unverified" string
    verif = offer.get("verification")
    if isinstance(verif, str):
        lower = verif.lower()
        if lower == "verified":
            return True
        if lower in ("unverified", "pending", "none", ""):
            return False
    return None  # unknown — caller decides based on reliability fallback


def _extract_offers_from_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract the list of GPU offers from a vast.ai API response.
    Tries common top-level keys: offers, results, bundles, data, list.
    Logs available keys when the schema is unexpected.
    """
    for key in ("offers", "results", "bundles", "data", "list"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    # Unknown schema — log top-level keys to help debugging
    top_keys = list(data.keys())[:10]
    logger.warning("vast_response_unexpected_schema top_level_keys=%s", top_keys)
    return []


def _vast_search_offers(min_vram_gb: int, gpu_name: str) -> Optional[Dict[str, Any]]:
    """
    Search vast.ai for the cheapest available GPU offer matching requirements.

    vast.ai /bundles/ does NOT accept order_by/order/sort query params —
    they cause HTTP 400.  We fetch all available offers and filter/sort
    in Python instead.

    Returns the best (cheapest dph_total) matching offer dict, or None.
    """
    # Only the 'q' JSON filter and 'api_key' are sent — no order/sort params.
    query: Dict[str, Any] = {
        "gpu_ram": {"gte": min_vram_gb},
        "rentable": {"eq": True},
    }
    if gpu_name:
        query["gpu_name"] = {"eq": gpu_name}

    params = {
        "q": json.dumps(query),
        "api_key": _VAST_API_KEY,   # token — not logged elsewhere
    }

    try:
        import requests  # type: ignore

        resp = requests.get(
            f"{_VAST_API_BASE}/bundles/",
            params=params,
            timeout=30,
        )

        if not resp.ok:
            logger.warning(
                "vast_search_failed http=%d body=%s", resp.status_code, resp.text[:300]
            )
            return None

        data = resp.json()
        offers = _extract_offers_from_response(data)

        logger.info("vast_search_raw_offers count=%d", len(offers))
        if offers:
            logger.debug(
                "vast_offer_first_keys keys=%s", list(offers[0].keys())[:15]
            )

        # ── Python-side filtering ─────────────────────────────────────────────
        filtered: List[Dict[str, Any]] = []
        for offer in offers:
            oid = offer.get("id", "?")
            gpu_label = _get_offer_gpu_name(offer)

            # VRAM check (field may be GB or MB)
            gpu_ram = offer.get("gpu_ram") or offer.get("gpu_ram_free_mb", 0)
            if isinstance(gpu_ram, (int, float)) and gpu_ram > 1000:
                gpu_ram = gpu_ram / 1024  # MB → GB
            if isinstance(gpu_ram, (int, float)) and gpu_ram < min_vram_gb:
                logger.debug(
                    "vast_offer_skip id=%s gpu=%r reason=vram(%.1f)<min(%d)",
                    oid, gpu_label, gpu_ram, min_vram_gb,
                )
                continue

            # Rentable — skip only if explicitly False
            if offer.get("rentable") is False:
                logger.debug("vast_offer_skip id=%s gpu=%r reason=not_rentable", oid, gpu_label)
                continue

            # Verified / reliability check
            # Unverified hosts hang at "Loading" / "Verifying checksum" and never
            # reach the backend API.  Use multi-field extraction to handle all API versions.
            if _VAST_REQUIRE_VERIFIED:
                is_verified = _check_offer_verified(offer)
                reliability  = _get_offer_reliability(offer)

                if is_verified is False:
                    # Host is explicitly marked unverified — reject regardless of reliability
                    logger.debug(
                        "vast_offer_skip id=%s gpu=%r reliability=%.1f reason=not_verified",
                        oid, gpu_label, reliability or 0.0,
                    )
                    continue

                if is_verified is None:
                    # Verification status unknown — fall back to reliability threshold
                    if reliability is None or reliability < _VAST_MIN_RELIABILITY:
                        logger.debug(
                            "vast_offer_skip id=%s gpu=%r reliability=%s reason=low_reliability_no_verified_field",
                            oid, gpu_label, f"{reliability:.1f}" if reliability is not None else "N/A",
                        )
                        continue
                else:
                    # is_verified is True — still check reliability if available
                    if reliability is not None and reliability < _VAST_MIN_RELIABILITY:
                        logger.debug(
                            "vast_offer_skip id=%s gpu=%r reliability=%.1f reason=low_reliability",
                            oid, gpu_label, reliability,
                        )
                        continue

            # GPU model exclusion list (legacy data-center GPUs)
            if gpu_label and _vast_exclude_re and _vast_exclude_re.search(gpu_label):
                logger.debug(
                    "vast_offer_skip id=%s gpu=%r reason=exclude_regex(%r)",
                    oid, gpu_label, _VAST_GPU_EXCLUDE_REGEX,
                )
                continue

            # GPU model inclusion list (require consumer/prosumer RTX / Ada / Ampere)
            if gpu_label and _vast_include_re and not _vast_include_re.search(gpu_label):
                logger.debug(
                    "vast_offer_skip id=%s gpu=%r reason=not_in_include_regex",
                    oid, gpu_label,
                )
                continue

            # Location exclusion list (KR/CN and other unstable regions by default)
            loc_label = _get_offer_location_label(offer)
            if loc_label and _vast_location_exclude_re and _vast_location_exclude_re.search(loc_label):
                logger.debug(
                    "vast_offer_skip id=%s gpu=%r loc=%r reason=location_exclude",
                    oid, gpu_label, loc_label,
                )
                continue

            # Location inclusion list (optional: restrict to US/EU/JP etc.)
            if loc_label and _vast_location_include_re and not _vast_location_include_re.search(loc_label):
                logger.debug(
                    "vast_offer_skip id=%s gpu=%r loc=%r reason=location_not_include",
                    oid, gpu_label, loc_label,
                )
                continue

            filtered.append(offer)

        if not filtered:
            logger.warning(
                "vast_no_matching_offers total=%d min_vram=%dGB gpu_name=%r",
                len(offers), min_vram_gb, gpu_name,
            )
            return None

        # ── Sort by price (cheapest first) ────────────────────────────────────
        def _price_key(o: Dict[str, Any]) -> float:
            for price_field in ("dph_total", "dph_base", "price", "cost_per_hour"):
                v = o.get(price_field)
                if isinstance(v, (int, float)):
                    return float(v)
            return float("inf")

        filtered.sort(key=_price_key)
        best = filtered[0]

        best_reliability = _get_offer_reliability(best)
        best_verified    = _check_offer_verified(best)
        logger.info(
            "vast_offer_selected id=%s gpu=%s vram=%s dph=%.4f verified=%s reliability=%s loc=%r",
            best.get("id"),
            best.get("gpu_name", "?"),
            best.get("gpu_ram", "?"),
            _price_key(best),
            best_verified,
            f"{best_reliability:.1f}" if best_reliability is not None else "N/A",
            _get_offer_location_label(best) or "unknown",
        )
        return best

    except Exception as exc:
        logger.warning("vast_search_error exc=%s", exc)
        return None


def _trigger_vast(job_id: str, mode: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Search for a GPU offer on vast.ai and create an ephemeral instance.

    The instance uses WORKER_BACKEND_MODE=api — it never touches PostgreSQL.
    All job operations go through BACKEND_API_URL; files go to/from S3.

    Deployment paths
    ----------------
    Direct image  (VAST_WORKER_IMAGE set — production):
        runtype=args — vast.ai runs the container directly as a one-shot job.
        The Docker ENTRYPOINT (/entrypoint.sh) is invoked via bash -lc.
        NO openssh-server installation, NO SSH wrapper, NO interactive mode.
        Env vars pass via the ``env`` dict field (HTTPS to vast.ai, never logged).
        args_str contains only the entrypoint command — no secrets.

    Git-clone fallback  (VAST_WORKER_IMAGE not set — public repos / dev only):
        runtype=ssh — used only for dev/debug, not production.
        Boots bare CUDA image; onstart is a base64-encoded bash script that
        git-clones the repo and calls bootstrap_worker_once.sh.
    """
    if not _VAST_API_KEY:
        err = "vast mode requires VAST_API_KEY"
        logger.error("vast_config_error job_id=%s %s", job_id, err)
        return False, {"error": err}
    if not os.environ.get("BACKEND_API_URL"):
        err = "vast mode requires BACKEND_API_URL (worker uses api backend mode)"
        logger.error("vast_config_error job_id=%s %s", job_id, err)
        return False, {"error": err}
    if not os.environ.get("WORKER_SECRET"):
        err = "vast mode requires WORKER_SECRET"
        logger.error("vast_config_error job_id=%s %s", job_id, err)
        return False, {"error": err}

    job_short      = job_id[:8]
    instance_label = f"{_VAST_LABEL_PREFIX}-{job_short}"

    if _VAST_WORKER_IMAGE:
        # ── Direct image mode (production) ────────────────────────────────────
        # runtype=args: vast.ai runs the container as a direct job — no SSH daemon,
        # no openssh-server, no interactive wrapper.  The image ENTRYPOINT is
        # called via bash -lc which gives a clean login-shell environment.
        #
        # Secrets go in `env` dict (transmitted over HTTPS to vast.ai API,
        # NEVER written to logs or embedded in args_str).
        env_dict        = _build_vast_env_dict(job_id, mode)
        effective_image = _VAST_WORKER_IMAGE
        deployment_mode = "direct-image-args"
        # args_str: safe to log — no secrets here, only the entrypoint path
        _args_str       = "bash -lc /entrypoint.sh"
        payload_fields: Dict[str, Any] = {
            "runtype":  "args",
            "args_str": _args_str,
            "env":      env_dict,   # secrets carried over HTTPS, not in any script
        }
        env_forwarded_keys = sorted(env_dict.keys())
    else:
        # ── Git-clone fallback (public repos / dev only, NOT production) ──────
        # runtype=ssh: used only when no pre-built image is available.
        # The base64-wrapped onstart script installs deps, clones the public repo,
        # and calls bootstrap_worker_once.sh.
        startup_script     = _build_vast_startup_script(job_id, mode)
        effective_image    = _VAST_IMAGE
        deployment_mode    = "git-clone-ssh"
        env_dict           = {}
        env_forwarded_keys = []
        payload_fields = {
            "runtype":  "ssh",
            "onstart":  _wrap_vast_startup_command(startup_script),
        }

    # Safe for logging — no secret values, no args content containing secrets
    sanitized_config: Dict[str, Any] = {
        "provider":               "vast",
        "instance_label":         instance_label,
        "effective_image":        effective_image,
        "deployment_mode":        deployment_mode,
        "runtype":                payload_fields["runtype"],
        # args_str is safe to log (no secrets; env dict carries them separately)
        "args_str":               payload_fields.get("args_str", "(n/a — ssh/onstart mode)"),
        "gpu_name_filter":        _VAST_GPU_NAME or "(any)",
        "gpu_include_regex":      _VAST_GPU_INCLUDE_REGEX or "(none)",
        "gpu_exclude_regex":      _VAST_GPU_EXCLUDE_REGEX or "(none)",
        "location_include_regex": _VAST_LOCATION_INCLUDE_REGEX or "(none)",
        "location_exclude_regex": _VAST_LOCATION_EXCLUDE_REGEX or "(none)",
        "require_verified":       _VAST_REQUIRE_VERIFIED,
        "min_reliability":        _VAST_MIN_RELIABILITY,
        "min_vram_gb":            _VAST_GPU_MIN_VRAM,
        "disk_gb":                _VAST_DISK_GB,
        "worker_backend_mode":    "api",
        "env_vars_forwarded":     env_forwarded_keys,  # key names only, no values
        "startup_preview":        _sanitized_startup_preview(job_id, mode),
    }

    # ── Search for a GPU offer (also in dry-run to show chosen offer) ─────────
    logger.info(
        "vast_searching_offers job_id=%s min_vram=%dGB gpu=%r image=%s mode=%s dry_run=%s",
        job_id, _VAST_GPU_MIN_VRAM, _VAST_GPU_NAME or "any",
        effective_image, deployment_mode, _VAST_DRY_RUN,
    )
    offer = _vast_search_offers(_VAST_GPU_MIN_VRAM, _VAST_GPU_NAME)
    if not offer:
        return False, {
            "ok": False, "provider": "vast",
            "error": "No suitable GPU offers found on vast.ai",
        }

    ask_id = offer.get("id")

    # ── Dry-run: show chosen offer, skip instance creation ────────────────────
    if _VAST_DRY_RUN:
        _rel = _get_offer_reliability(offer)
        _ver = _check_offer_verified(offer)
        sanitized_config["chosen_offer"] = {
            "id":          str(ask_id),
            "gpu_name":    offer.get("gpu_name", "?"),
            "gpu_ram":     offer.get("gpu_ram", "?"),
            "dph":         offer.get("dph_total", offer.get("dph_base", "?")),
            "verified":    _ver,
            "reliability": f"{_rel:.1f}" if _rel is not None else "N/A",
            "location":    _get_offer_location_label(offer) or "unknown",
        }
        logger.info(
            "vast_dry_run job_id=%s label=%s config=%s",
            job_id, instance_label, json.dumps(sanitized_config, indent=2),
        )
        return True, {
            "ok": True, "provider": "vast", "dry_run": True,
            "instance_label": instance_label,
            "sanitized_config": sanitized_config,
        }

    # ── Create instance ────────────────────────────────────────────────────────
    # Direct image (production):
    #   runtype=args + args_str="bash -lc /entrypoint.sh" + env dict
    #   → No SSH daemon, no openssh-server, no interactive wrapper.
    #   → Container starts and runs the worker ENTRYPOINT as a one-shot job.
    # Git-clone fallback (dev/debug):
    #   runtype=ssh + onstart=base64-wrapped bash script.
    instance_payload: Dict[str, Any] = {
        "client_id": "me",
        "image":     effective_image,
        "disk":      _VAST_DISK_GB,
        "label":     instance_label,
        **payload_fields,   # runtype + (args_str+env | onstart) per deployment mode
    }
    logger.info(
        "vast_create_instance job_id=%s ask_id=%s label=%s image=%s runtype=%s mode=%s",
        job_id, ask_id, instance_label, effective_image,
        payload_fields["runtype"], deployment_mode,
    )
    try:
        import requests  # type: ignore

        resp = requests.put(
            f"{_VAST_API_BASE}/asks/{ask_id}/",
            params={"api_key": _VAST_API_KEY},
            json=instance_payload,
            timeout=30,
        )
        try:
            raw_body = resp.json()
        except Exception:
            raw_body = {"raw_text": resp.text[:500]}

        sanitized_body = _sanitize_api_response(raw_body) if isinstance(raw_body, dict) else raw_body

        if resp.ok and raw_body.get("success"):
            contract_id = raw_body.get("new_contract", "")
            logger.info(
                "vast_instance_created job_id=%s contract_id=%s label=%s offer_gpu=%s",
                job_id, contract_id, instance_label, offer.get("gpu_name"),
            )
            return True, {
                "ok": True,
                "provider": "vast",
                "contract_id": str(contract_id),
                "instance_label": instance_label,
                "offer_id": str(ask_id),
                "offer_gpu": offer.get("gpu_name", ""),
                "offer_vram_gb": offer.get("gpu_ram", ""),
                "offer_dph": offer.get("dph_total", ""),
                "raw_response": sanitized_body,
            }

        logger.warning(
            "vast_api_error job_id=%s http=%d body=%s",
            job_id, resp.status_code, json.dumps(sanitized_body)[:300],
        )
        return False, {
            "ok": False, "provider": "vast",
            "error": f"vast.ai API HTTP {resp.status_code}",
            "raw_response": sanitized_body,
        }
    except Exception as exc:
        logger.warning("vast_exception job_id=%s exc=%s", job_id, exc)
        return False, {"ok": False, "provider": "vast", "error": str(exc)}


# ── Public API ─────────────────────────────────────────────────────────────────

def trigger_gpu_for_job(
    job_id: str,
    mode: str,
    priority: int = 100,
    plan: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Request an ephemeral GPU instance for a single job.

    Returns:
        (True,  result_dict)    — request accepted / instance created
        (False, {"error": ...}) — disabled, misconfigured, or API/network error

    Never raises.  API tokens and secrets are never logged.
    """
    if _MODE == "disabled":
        logger.info("gpu_orchestrator disabled job_id=%s", job_id)
        return False, {"error": "GPU_ORCHESTRATOR_MODE=disabled"}

    if _MODE == "webhook":
        return _trigger_webhook(job_id, mode, priority, plan, extra)

    if _MODE == "timeweb":
        return _trigger_timeweb(job_id, mode)

    if _MODE == "vast":
        return _trigger_vast(job_id, mode)

    logger.error("gpu_orchestrator unknown mode=%r job_id=%s", _MODE, job_id)
    return False, {"error": f"Unknown GPU_ORCHESTRATOR_MODE={_MODE!r}"}


def verify_orchestrator_signature(body: bytes, signature: str) -> bool:
    """
    Verify an incoming webhook callback from the GPU provider back to the API.
    Use on the API side when exposing a GPU-callback endpoint.
    """
    if not _WEBHOOK_SECRET:
        return False
    expected = _sign_payload(body)
    return hmac.compare_digest(expected, signature)
