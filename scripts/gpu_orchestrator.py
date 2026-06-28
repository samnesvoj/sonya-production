"""
gpu_orchestrator.py
===================
Ephemeral GPU provisioning — multi-mode.

GPU_ORCHESTRATOR_MODE selects which backend creates the GPU instance:

  disabled  — nothing is created (safe default)
  webhook   — sends a signed HMAC webhook to an external orchestrator (n8n etc.)
  timeweb   — directly calls Timeweb Cloud API; no n8n / external service needed

This module is NOT a worker and does NOT generate video.
It only requests creation of a temporary GPU instance for a single job.
The instance runs deploy/gpu/bootstrap_worker_once.sh, processes the job,
uploads the result to S3, then shuts itself down.

────────────────────────────────────────────────────────────────────────────────
Common env vars
────────────────────────────────────────────────────────────────────────────────
  GPU_ORCHESTRATOR_MODE       disabled | webhook | timeweb   (default disabled)
  SHUTDOWN_AFTER_JOB          true | false                   (default true)
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
timeweb mode env vars
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
                              (default deploy/gpu/bootstrap_worker_once.sh)

  # Forwarded to the GPU instance via cloud-init user-data (never logged):
  DATABASE_URL        S3_ENDPOINT_URL     S3_ACCESS_KEY_ID  S3_SECRET_ACCESS_KEY
  S3_BUCKET_NAME      S3_REGION           MODELS_S3_BUCKET  WORKER_SECRET
  GEMINI_API_KEY      OPENROUTER_API_KEY  ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID
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

# ── Timeweb mode config ────────────────────────────────────────────────────────

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

# Env vars forwarded to the GPU instance via cloud-init (may contain secrets)
_GPU_WORKER_ENV_VARS: List[str] = [
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

# These are never printed in logs or sanitized payloads
_SECRET_ENV_VARS = frozenset({
    "DATABASE_URL",
    "S3_SECRET_ACCESS_KEY",
    "WORKER_SECRET",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sign_payload(body: bytes) -> str:
    """HMAC-SHA256 over the raw JSON body. Secret is never logged."""
    if not _WEBHOOK_SECRET:
        logger.warning("GPU_ORCHESTRATOR_WEBHOOK_SECRET not set — webhook unsigned")
        return ""
    return hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _sanitized_env_map(job_id: str, mode: str) -> Dict[str, str]:
    """
    Return {VAR: value_or_masked} for logging / dry-run display.
    Secret values are replaced with ***.
    """
    result: Dict[str, str] = {"JOB_ID": job_id, "MODE": mode}
    for var in _GPU_WORKER_ENV_VARS:
        val = os.environ.get(var, "")
        if not val:
            continue
        result[var] = "***" if var in _SECRET_ENV_VARS else val
    return result


def _build_cloud_init(job_id: str, mode: str) -> str:
    """
    Build a cloud-init user-data shell script that is injected into the
    Timeweb GPU instance.  The script:
      1. Exports all required env vars (secrets embedded — not logged elsewhere)
      2. Installs git and clones the repo
      3. Delegates to bootstrap_worker_once.sh inside the repo

    This is intentionally a minimal launcher; all real logic lives in bootstrap.
    """
    lines = [
        "#!/usr/bin/env bash",
        "# SONYA ephemeral GPU cloud-init — generated by gpu_orchestrator.py",
        f"# job_id={job_id}  mode={mode}",
        "set -euo pipefail",
        "",
        "# ── Environment ──────────────────────────────────────────────────────",
        f'export JOB_ID="{job_id}"',
        f'export MODE="{mode}"',
    ]

    for var in _GPU_WORKER_ENV_VARS:
        val = os.environ.get(var, "")
        if val:
            # Single-quote with inner-single-quote escaping to avoid injection
            safe = val.replace("'", "'\\''")
            lines.append(f"export {var}='{safe}'")

    lines += [
        "",
        "# ── Bootstrap ────────────────────────────────────────────────────────",
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -qq",
        "apt-get install -y -qq git",
        "",
        f'INSTALL_DIR="/opt/sonya"',
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

    return "\n".join(lines) + "\n"


def _build_timeweb_server_payload(
    job_id: str,
    mode: str,
    instance_name: str,
) -> Dict[str, Any]:
    """Build the Timeweb Cloud API request body for POST /api/v1/servers."""
    user_data = _build_cloud_init(job_id, mode)
    # Timeweb accepts raw cloud-init or base64; base64 avoids quoting issues
    user_data_b64 = base64.b64encode(user_data.encode()).decode()

    payload: Dict[str, Any] = {
        "name": instance_name,
        "preset_id": int(_TW_PRESET_ID) if _TW_PRESET_ID.isdigit() else _TW_PRESET_ID,
        "os_id": int(_TW_IMAGE_ID) if _TW_IMAGE_ID.isdigit() else _TW_IMAGE_ID,
        "user_data": user_data_b64,
    }

    if _TW_REGION:
        payload["location"] = _TW_REGION
    if _TW_PROJECT_ID:
        payload["project_id"] = _TW_PROJECT_ID
    if _TW_SSH_KEY_ID:
        payload["ssh_keys_ids"] = [int(_TW_SSH_KEY_ID) if _TW_SSH_KEY_ID.isdigit() else _TW_SSH_KEY_ID]
    if _TW_NETWORK_ID:
        payload["networks"] = [{"id": _TW_NETWORK_ID, "type": "private"}]

    return payload


def _sanitize_timeweb_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any token/secret fields that might appear in the API response."""
    _sensitive = {"password", "token", "secret", "key", "access"}
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if any(s in k.lower() for s in _sensitive):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _sanitize_timeweb_response(v)
        else:
            out[k] = v
    return out


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


# ── Timeweb mode ───────────────────────────────────────────────────────────────

def _trigger_timeweb(
    job_id: str,
    mode: str,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Create an ephemeral GPU server on Timeweb Cloud for a single job.
    Returns (ok, result_dict).  Secrets are never logged.
    """
    job_short = job_id[:8]
    instance_name = f"{_TW_NAME_PREFIX}-{job_short}"

    # ── Config validation ──────────────────────────────────────────────────────
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

    server_payload = _build_timeweb_server_payload(job_id, mode, instance_name)

    # ── Sanitized log / dry-run payload ───────────────────────────────────────
    sanitized_config = {
        "provider": "timeweb",
        "instance_name": instance_name,
        "preset_id": _TW_PRESET_ID,
        "image_id": _TW_IMAGE_ID,
        "region": _TW_REGION or "(not set)",
        "project_id": _TW_PROJECT_ID or "(not set)",
        "ssh_key_id": _TW_SSH_KEY_ID or "(not set)",
        "network_id": _TW_NETWORK_ID or "(not set)",
        "delete_after_job": _TW_DELETE_AFTER,
        "shutdown_after_job": SHUTDOWN_AFTER_JOB,
        "bootstrap_path": _BOOTSTRAP_PATH,
        "env_forwarded": _sanitized_env_map(job_id, mode),
        "user_data_bytes": len(_build_cloud_init(job_id, mode)),
    }

    # ── Dry-run mode ───────────────────────────────────────────────────────────
    if _TW_DRY_RUN:
        logger.info(
            "timeweb_dry_run job_id=%s instance=%s payload=%s",
            job_id, instance_name, json.dumps(sanitized_config, indent=2),
        )
        return True, {
            "ok": True,
            "provider": "timeweb",
            "dry_run": True,
            "instance_name": instance_name,
            "sanitized_config": sanitized_config,
        }

    # ── Live API call ──────────────────────────────────────────────────────────
    logger.info(
        "timeweb_create_server job_id=%s instance=%s preset=%s image=%s region=%s",
        job_id, instance_name, _TW_PRESET_ID, _TW_IMAGE_ID, _TW_REGION,
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_TW_API_TOKEN}",  # token never reaches logs
    }

    try:
        import requests  # type: ignore

        resp = requests.post(
            f"{_TW_API_BASE}/servers",
            json=server_payload,
            headers=headers,
            timeout=30,
        )

        # Parse and sanitize the response body
        try:
            raw_body = resp.json()
        except Exception:
            raw_body = {"raw_text": resp.text[:500]}

        sanitized_body = _sanitize_timeweb_response(raw_body) if isinstance(raw_body, dict) else raw_body

        if resp.ok:
            server_data = raw_body.get("server", raw_body)
            instance_id = str(server_data.get("id", ""))
            status_val  = server_data.get("status", "")
            logger.info(
                "timeweb_server_created job_id=%s instance_id=%s name=%s status=%s",
                job_id, instance_id, instance_name, status_val,
            )
            return True, {
                "ok": True,
                "provider": "timeweb",
                "instance_id": instance_id,
                "instance_name": instance_name,
                "status": status_val,
                "delete_after_job": _TW_DELETE_AFTER,
                "raw_response": sanitized_body,
            }

        logger.warning(
            "timeweb_api_error job_id=%s http=%d body=%s",
            job_id, resp.status_code, json.dumps(sanitized_body)[:300],
        )
        return False, {
            "ok": False,
            "provider": "timeweb",
            "error": f"Timeweb API HTTP {resp.status_code}",
            "raw_response": sanitized_body,
        }

    except Exception as exc:
        logger.warning("timeweb_exception job_id=%s exc=%s", job_id, exc)
        return False, {"ok": False, "provider": "timeweb", "error": str(exc)}


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

    logger.error("gpu_orchestrator unknown mode=%s job_id=%s", _MODE, job_id)
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
