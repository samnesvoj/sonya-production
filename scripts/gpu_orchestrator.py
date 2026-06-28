"""
gpu_orchestrator.py
===================
Ephemeral GPU provisioning via signed webhook.

This module is NOT a worker and does NOT generate video.
Its only responsibility is sending a signed webhook that instructs an
external provider (n8n, cloud API, etc.) to create a temporary GPU instance
for a single job.  The GPU instance runs bootstrap_worker_once.sh, processes
the job, then shuts itself down.

Env:
    GPU_ORCHESTRATOR_MODE           webhook | disabled  (default disabled)
    GPU_ORCHESTRATOR_WEBHOOK_URL    https://n8n.sonya-e.com/webhook/gpu-trigger
    GPU_ORCHESTRATOR_WEBHOOK_SECRET HMAC signing secret (never logged)
    GPU_INSTANCE_TYPE               e.g. A100, RTX4090
    GPU_IMAGE                       e.g. ubuntu-22.04-cuda-12-2
    GPU_REGION                      e.g. eu-central-1
    SHUTDOWN_AFTER_JOB              true | false (default true)
    BACKEND_API_URL                 https://sonya-e.com (sent to worker in payload)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

_MODE = os.environ.get("GPU_ORCHESTRATOR_MODE", "disabled").lower()
_WEBHOOK_URL = os.environ.get("GPU_ORCHESTRATOR_WEBHOOK_URL", "")
_WEBHOOK_SECRET = os.environ.get("GPU_ORCHESTRATOR_WEBHOOK_SECRET", "")

GPU_INSTANCE_TYPE = os.environ.get("GPU_INSTANCE_TYPE", "")
GPU_IMAGE = os.environ.get("GPU_IMAGE", "")
GPU_REGION = os.environ.get("GPU_REGION", "")
SHUTDOWN_AFTER_JOB: bool = os.environ.get("SHUTDOWN_AFTER_JOB", "true").lower() == "true"
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "https://sonya-e.com")

# Legacy read (some callers still use AUTO_GPU_TRIGGER_ENABLED to detect if GPU is on)
AUTO_GPU_TRIGGER_ENABLED: bool = (
    os.environ.get("AUTO_GPU_TRIGGER_ENABLED", "false").lower() == "true"
)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _sign_payload(body: bytes) -> str:
    """HMAC-SHA256 signature over the raw JSON body. Secret never logged."""
    if not _WEBHOOK_SECRET:
        logger.warning("GPU_ORCHESTRATOR_WEBHOOK_SECRET not set — webhook unsigned")
        return ""
    return hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _build_payload(
    job_id: str,
    mode: str,
    priority: int,
    plan: Optional[str],
) -> Dict[str, Any]:
    return {
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


# ── Public API ─────────────────────────────────────────────────────────────────

def trigger_gpu_for_job(
    job_id: str,
    mode: str,
    priority: int = 100,
    plan: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Send a signed GPU provisioning webhook to the external orchestrator (n8n).

    Returns:
        (True,  payload_dict)   — webhook accepted (2xx)
        (False, {"error": ...}) — disabled, misconfigured, or HTTP/network error

    Never raises.  Secrets are never logged.
    """
    if _MODE == "disabled":
        logger.info("gpu_orchestrator disabled job_id=%s", job_id)
        return False, {"error": "GPU_ORCHESTRATOR_MODE=disabled"}

    if not _WEBHOOK_URL:
        logger.warning(
            "GPU_ORCHESTRATOR_WEBHOOK_URL not set — cannot trigger GPU job_id=%s",
            job_id,
        )
        return False, {"error": "GPU_ORCHESTRATOR_WEBHOOK_URL not configured"}

    payload = _build_payload(job_id, mode, priority, plan)
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
                "gpu_webhook_ok job_id=%s mode=%s http=%d",
                job_id, mode, resp.status_code,
            )
            return True, payload
        else:
            logger.warning(
                "gpu_webhook_failed job_id=%s mode=%s http=%d",
                job_id, mode, resp.status_code,
            )
            return False, {"error": f"HTTP {resp.status_code}"}

    except Exception as exc:
        logger.warning("gpu_webhook_error job_id=%s exc=%s", job_id, exc)
        return False, {"error": str(exc)}


def verify_orchestrator_signature(body: bytes, signature: str) -> bool:
    """
    Verify an incoming webhook callback from the GPU provider back to the API.
    Use on the API side if you expose a GPU-callback endpoint.
    """
    if not _WEBHOOK_SECRET:
        return False
    expected = _sign_payload(body)
    return hmac.compare_digest(expected, signature)
