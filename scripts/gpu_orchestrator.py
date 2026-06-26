"""
gpu_orchestrator.py
===================
Orchestration layer for managing GPU worker instances.
Designed for n8n / external trigger integration.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
_WORKER = _ROOT / "scripts" / "gpu_worker.py"


def dispatch_job(job_id: str) -> subprocess.Popen:
    """
    Launch a detached gpu_worker process for a single job.
    Returns the Popen handle.
    """
    cmd = [sys.executable, str(_WORKER), "--once", "--job-id", job_id]
    logger.info("Dispatching worker: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(_ROOT),
    )
    return proc


def launch_poll_worker() -> subprocess.Popen:
    """Launch a long-running poll worker."""
    cmd = [sys.executable, str(_WORKER), "--poll"]
    logger.info("Launching poll worker")
    return subprocess.Popen(cmd, cwd=str(_ROOT))
