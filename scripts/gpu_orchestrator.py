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
  VAST_GPU_NAME               optional GPU model filter (e.g. RTX4090, A100)
  VAST_GPU_MIN_VRAM           minimum VRAM in GB (default 24)
  VAST_DISK_GB                disk size in GB (default 50)
  VAST_INSTANCE_LABEL_PREFIX  label prefix for created instances (default sonya-gpu)
  VAST_DRY_RUN                true → log sanitized payload, skip API call

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
_VAST_GPU_NAME          = os.environ.get("VAST_GPU_NAME", "")      # optional GPU model filter
_VAST_GPU_MIN_VRAM      = int(os.environ.get("VAST_GPU_MIN_VRAM", "24"))
_VAST_DISK_GB           = int(os.environ.get("VAST_DISK_GB", "50"))
_VAST_LABEL_PREFIX      = os.environ.get("VAST_INSTANCE_LABEL_PREFIX", "sonya-gpu")
_VAST_DRY_RUN           = os.environ.get("VAST_DRY_RUN", "false").lower() == "true"

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

def _build_vast_startup_script(job_id: str, mode: str) -> str:
    """
    Build the vast.ai instance startup (onstart) script.

    The script:
      1. Exports all required env vars (secrets embedded, not logged elsewhere)
      2. Sets WORKER_BACKEND_MODE=api — no DATABASE_URL used by the worker
      3. Installs system dependencies
      4. Clones the repo
      5. Delegates to bootstrap_worker_once.sh
    """
    env_lines = _build_env_export_lines(
        job_id, mode,
        extra_exports={"WORKER_BACKEND_MODE": "api"},
        var_list=_VAST_WORKER_ENV_VARS,
    )
    script_lines = [
        "#!/usr/bin/env bash",
        "# SONYA vast.ai GPU worker startup — generated by gpu_orchestrator.py",
        f"# job_id={job_id}  mode={mode}",
        "set -euo pipefail",
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
        f'INSTALL_DIR="/opt/sonya"',
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
    return "\n".join(script_lines) + "\n"


def _vast_search_offers(min_vram_gb: int, gpu_name: str) -> Optional[Dict[str, Any]]:
    """
    Search vast.ai for the cheapest available GPU offer matching requirements.
    Returns the best offer dict, or None if nothing found.
    """
    query: Dict[str, Any] = {
        "gpu_ram":   {"gte": min_vram_gb},
        "rentable":  {"eq": True},
        "verified":  {"eq": True},
        "cuda_max_good": {"gte": 12.0},
    }
    if gpu_name:
        query["gpu_name"] = {"eq": gpu_name}

    params = {
        "q": json.dumps(query),
        "order_by": "dph_total",
        "order": "asc",
        "api_key": _VAST_API_KEY,
    }
    try:
        import requests  # type: ignore

        resp = requests.get(
            f"{_VAST_API_BASE}/bundles/",
            params=params,
            timeout=20,
        )
        if not resp.ok:
            logger.warning("vast_search_failed http=%d body=%s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        offers = data.get("offers", [])
        if not offers:
            logger.warning("vast_no_offers min_vram=%dGB gpu_name=%r", min_vram_gb, gpu_name)
            return None
        best = offers[0]
        logger.info(
            "vast_offer_selected id=%s gpu=%s vram=%sGB dph=%.4f",
            best.get("id"), best.get("gpu_name"), best.get("gpu_ram"), best.get("dph_total", 0),
        )
        return best
    except Exception as exc:
        logger.warning("vast_search_error exc=%s", exc)
        return None


def _trigger_vast(job_id: str, mode: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Search for a GPU offer on vast.ai and create an ephemeral instance.

    The instance uses WORKER_BACKEND_MODE=api so it never connects to
    PostgreSQL directly — all job operations go through BACKEND_API_URL.
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
    startup_script = _build_vast_startup_script(job_id, mode)

    sanitized_config = {
        "provider": "vast",
        "instance_label": instance_label,
        "image": _VAST_IMAGE,
        "gpu_name_filter": _VAST_GPU_NAME or "(any)",
        "min_vram_gb": _VAST_GPU_MIN_VRAM,
        "disk_gb": _VAST_DISK_GB,
        "worker_backend_mode": "api",
        "env_forwarded": _sanitized_env_map(job_id, mode, _VAST_WORKER_ENV_VARS),
        "startup_script_bytes": len(startup_script),
    }

    # ── Dry-run ────────────────────────────────────────────────────────────────
    if _VAST_DRY_RUN:
        logger.info(
            "vast_dry_run job_id=%s label=%s config=%s",
            job_id, instance_label, json.dumps(sanitized_config, indent=2),
        )
        return True, {
            "ok": True, "provider": "vast", "dry_run": True,
            "instance_label": instance_label,
            "sanitized_config": sanitized_config,
        }

    # ── Search for a GPU offer ─────────────────────────────────────────────────
    logger.info(
        "vast_searching_offers job_id=%s min_vram=%dGB gpu=%r image=%s",
        job_id, _VAST_GPU_MIN_VRAM, _VAST_GPU_NAME or "any", _VAST_IMAGE,
    )
    offer = _vast_search_offers(_VAST_GPU_MIN_VRAM, _VAST_GPU_NAME)
    if not offer:
        return False, {
            "ok": False, "provider": "vast",
            "error": "No suitable GPU offers found on vast.ai",
        }

    ask_id = offer.get("id")

    # ── Create instance ────────────────────────────────────────────────────────
    instance_payload: Dict[str, Any] = {
        "client_id": "me",
        "image":     _VAST_IMAGE,
        "disk":      _VAST_DISK_GB,
        "onstart":   startup_script,
        "label":     instance_label,
        "runtype":   "args",
    }
    logger.info(
        "vast_create_instance job_id=%s ask_id=%s label=%s",
        job_id, ask_id, instance_label,
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
