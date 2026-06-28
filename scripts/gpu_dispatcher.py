"""
gpu_dispatcher.py
=================
Production queue dispatcher for SONYA.

Runs on the VPS as a systemd service.  Polls PostgreSQL for queued jobs and
calls gpu_orchestrator to request an ephemeral GPU instance for each one.

No GPU compute happens here.  The dispatcher only decides *when* to fire a
webhook; the GPU instance is created by the orchestrator (n8n / provider API)
and destroys itself automatically after the job finishes.

Usage:
    python scripts/gpu_dispatcher.py           # continuous loop (systemd)
    python scripts/gpu_dispatcher.py --once    # dispatch one job and exit

Env:
    AUTO_GPU_TRIGGER_ENABLED      true | false  (default false — safe off)
    GPU_DISPATCH_INTERVAL_SECONDS               poll interval    (default 20)
    MAX_ACTIVE_GPU_JOBS                         concurrency cap  (default 1)
    DATABASE_URL                                PostgreSQL DSN   (required)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.prod_job_store import (
    count_active_gpu_jobs,
    get_next_queued_job_for_dispatch,
    lock_job_for_dispatch,
    mark_gpu_request_failed,
    mark_gpu_requested,
)
import scripts.gpu_orchestrator as orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

AUTO_GPU_TRIGGER_ENABLED: bool = (
    os.environ.get("AUTO_GPU_TRIGGER_ENABLED", "false").lower() == "true"
)
DISPATCH_INTERVAL: int = int(os.environ.get("GPU_DISPATCH_INTERVAL_SECONDS", "20"))
MAX_ACTIVE_GPU_JOBS: int = int(os.environ.get("MAX_ACTIVE_GPU_JOBS", "1"))


# ── Core dispatch logic ────────────────────────────────────────────────────────

def _dispatch_one() -> bool:
    """
    Try to dispatch one queued job.

    Returns True if a job was found and dispatched (or attempted).
    Returns False if nothing available or concurrency cap reached.
    """
    active = count_active_gpu_jobs()
    if active >= MAX_ACTIVE_GPU_JOBS:
        logger.debug(
            "dispatch_skip active=%d max=%d", active, MAX_ACTIVE_GPU_JOBS
        )
        return False

    candidate = get_next_queued_job_for_dispatch()
    if not candidate:
        logger.debug("dispatch_skip no_queued_jobs")
        return False

    job_id: str  = str(candidate["id"])
    mode: str    = candidate.get("mode", "")
    priority: int = candidate.get("priority", 100)
    plan: str | None = candidate.get("plan")

    # Atomically acquire lock — prevents two dispatcher instances racing
    locked = lock_job_for_dispatch(job_id, lock_seconds=120)
    if not locked:
        logger.info("dispatch_race_lost job_id=%s", job_id)
        return False

    logger.info(
        "dispatching job_id=%s mode=%s priority=%d attempt=%d",
        job_id, mode, priority, locked.get("attempts", 1),
    )

    try:
        ok, payload = orchestrator.trigger_gpu_for_job(
            job_id=job_id,
            mode=mode,
            priority=priority,
            plan=plan,
        )
        if ok:
            mark_gpu_requested(job_id, orchestrator_payload=payload)
            logger.info("dispatch_ok job_id=%s", job_id)
        else:
            error = (
                payload.get("error", "orchestrator returned failure")
                if isinstance(payload, dict)
                else "orchestrator returned failure"
            )
            mark_gpu_request_failed(job_id, error)
            logger.warning("dispatch_failed job_id=%s error=%s", job_id, error)

    except Exception as exc:
        logger.error("dispatch_exception job_id=%s exc=%s", job_id, exc)
        mark_gpu_request_failed(job_id, f"{type(exc).__name__}: {exc}")

    return True


# ── Loop / once ────────────────────────────────────────────────────────────────

def run_loop() -> None:
    """Continuous dispatcher loop.  Runs until SIGTERM / KeyboardInterrupt."""
    if not AUTO_GPU_TRIGGER_ENABLED:
        logger.warning(
            "AUTO_GPU_TRIGGER_ENABLED=false — dispatcher in dry-run mode "
            "(logs candidates, no GPU triggered)"
        )

    logger.info(
        "dispatcher_start interval=%ds max_active=%d enabled=%s",
        DISPATCH_INTERVAL,
        MAX_ACTIVE_GPU_JOBS,
        AUTO_GPU_TRIGGER_ENABLED,
    )

    while True:
        try:
            if AUTO_GPU_TRIGGER_ENABLED:
                _dispatch_one()
            else:
                candidate = get_next_queued_job_for_dispatch()
                if candidate:
                    logger.info(
                        "dry_run would_dispatch job_id=%s mode=%s priority=%d",
                        candidate.get("id"),
                        candidate.get("mode"),
                        candidate.get("priority", 100),
                    )
        except Exception as exc:
            logger.error("dispatcher_loop_error exc=%s", exc)

        time.sleep(DISPATCH_INTERVAL)


def run_once() -> int:
    """Dispatch at most one job and exit.  Returns exit code."""
    if not AUTO_GPU_TRIGGER_ENABLED:
        logger.warning("AUTO_GPU_TRIGGER_ENABLED=false — no GPU will be triggered")
        candidate = get_next_queued_job_for_dispatch()
        if candidate:
            logger.info(
                "dry_run would_dispatch job_id=%s mode=%s priority=%d",
                candidate.get("id"),
                candidate.get("mode"),
                candidate.get("priority", 100),
            )
        return 0

    dispatched = _dispatch_one()
    logger.info("run_once dispatched=%s", dispatched)
    return 0


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="SONYA GPU dispatcher")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Dispatch one pending job and exit (instead of looping)",
    )
    args = parser.parse_args()

    if args.once:
        sys.exit(run_once())
    else:
        run_loop()


if __name__ == "__main__":
    main()
