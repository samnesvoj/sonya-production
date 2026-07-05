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

Production startup SLA (Vast cold-start protection):
    VAST_STARTUP_TIMEOUT_SEC       max sec from gpu_requested_at to
                                    worker_started_at            (default 240)
    VAST_MAX_STARTUP_RETRIES       max attempts before permanent fail (default 3)
    VAST_SLOW_HOST_COOLDOWN_MIN    blacklist cooldown for the offending
                                    host/machine/ip               (default 60)

    cleanup_stale_gpu_requests() runs before every dispatch pass (loop and
    --once). It destroys any Vast instance stuck past the timeout, blacklists
    the host, and requeues/fails the job so a single slow instance never
    blocks the production queue. See scripts/gpu_orchestrator.py and
    scripts/vast_bad_hosts.py for the full policy.
"""
from __future__ import annotations

import argparse
import json
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
    get_stale_gpu_requested_jobs,
    lock_job_for_dispatch,
    mark_gpu_request_failed,
    mark_gpu_requested,
    mark_gpu_startup_timeout,
)
import scripts.gpu_orchestrator as orchestrator
import scripts.vast_bad_hosts as vast_bad_hosts

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

# Production startup SLA — shared config lives in gpu_orchestrator.py so the
# dispatcher, orchestrator, and offer-blacklist filter all agree on one value.
VAST_STARTUP_TIMEOUT_SEC: int = orchestrator.VAST_STARTUP_TIMEOUT_SEC
VAST_MAX_STARTUP_RETRIES: int = orchestrator.VAST_MAX_STARTUP_RETRIES
VAST_SLOW_HOST_COOLDOWN_MIN: int = orchestrator.VAST_SLOW_HOST_COOLDOWN_MIN


# ── Production startup SLA enforcement ──────────────────────────────────────────

def cleanup_stale_gpu_requests() -> int:
    """
    Enforce the production Vast startup SLA.

    Finds jobs with status='gpu_requested' whose gpu_requested_at is older
    than VAST_STARTUP_TIMEOUT_SEC while worker_started_at is still NULL (the
    worker never reported in — e.g. a 4+ minute cold pull on a slow host).

    For each stale job:
      1. Destroy the Vast instance via contract_id from orchestrator_payload
         (best-effort — a destroy failure is logged but never blocks retry).
      2. Blacklist the offending host_id/machine_id/ip for
         VAST_SLOW_HOST_COOLDOWN_MIN minutes (scripts/vast_bad_hosts.py).
      3. Record error='vast startup timeout after N sec' and either requeue
         the job (attempts < retry cap) or permanently fail it.

    Must run before every dispatch pass so a retried job can land on a fresh,
    non-blacklisted offer on the very next loop iteration.

    Never raises — a single bad row must not crash the dispatcher loop.
    Returns the number of stale jobs processed.
    """
    try:
        stale_jobs = get_stale_gpu_requested_jobs(VAST_STARTUP_TIMEOUT_SEC)
    except Exception as exc:
        logger.error("cleanup_stale_gpu_requests_query_failed exc=%s", exc)
        return 0

    for job in stale_jobs:
        job_id = str(job.get("id"))
        try:
            _cleanup_one_stale_job(job)
        except Exception as exc:
            logger.error(
                "cleanup_stale_gpu_requests_job_error job_id=%s exc=%s", job_id, exc
            )

    return len(stale_jobs)


def _cleanup_one_stale_job(job: dict) -> None:
    job_id = str(job.get("id"))
    attempts = job.get("attempts", 0)
    max_attempts = job.get("max_attempts", VAST_MAX_STARTUP_RETRIES)

    payload = job.get("orchestrator_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    contract_id = str(payload.get("contract_id") or "")
    offer_id    = str(payload.get("offer_id") or "")
    offer_gpu   = str(payload.get("offer_gpu") or "")
    host_id     = str(payload.get("host_id") or "")
    machine_id  = str(payload.get("machine_id") or "")
    offer_ip    = str(payload.get("offer_ip") or "")

    logger.warning(
        "gpu_startup_timeout job_id=%s attempts=%s max_attempts=%s "
        "offer_id=%s offer_gpu=%s host_id=%s machine_id=%s",
        job_id, attempts, max_attempts, offer_id, offer_gpu, host_id, machine_id,
    )

    # 1) Destroy the stuck instance (best-effort; never blocks the job below).
    if contract_id:
        destroyed = orchestrator.destroy_vast_instance(contract_id)
        if not destroyed:
            logger.warning(
                "gpu_startup_timeout_destroy_failed job_id=%s contract_id=%s "
                "— proceeding with retry/fail anyway",
                job_id, contract_id,
            )
    else:
        logger.warning(
            "gpu_startup_timeout_no_contract_id job_id=%s — cannot destroy instance",
            job_id,
        )

    # 2) Blacklist the slow host/machine/ip for VAST_SLOW_HOST_COOLDOWN_MIN.
    if host_id or machine_id or offer_ip:
        vast_bad_hosts.add_bad_host(
            host_id=host_id or None,
            machine_id=machine_id or None,
            ip=offer_ip or None,
            offer_id=offer_id or None,
            reason="slow_startup",
            cooldown_minutes=VAST_SLOW_HOST_COOLDOWN_MIN,
        )
    else:
        logger.warning(
            "gpu_startup_timeout_no_host_identifiers job_id=%s — cannot blacklist host",
            job_id,
        )

    # 3) Requeue (another offer) or permanently fail.
    updated = mark_gpu_startup_timeout(
        job_id,
        error=f"vast startup timeout after {VAST_STARTUP_TIMEOUT_SEC} sec",
        max_startup_retries=VAST_MAX_STARTUP_RETRIES,
    )
    logger.info(
        "gpu_startup_timeout_resolved job_id=%s new_status=%s attempts=%s/%s",
        job_id, updated.get("status"), updated.get("attempts"), updated.get("max_attempts"),
    )


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
            cleaned = cleanup_stale_gpu_requests()
            if cleaned:
                logger.info("dispatcher_cleanup stale_jobs_processed=%d", cleaned)

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
    cleaned = cleanup_stale_gpu_requests()
    if cleaned:
        logger.info("dispatcher_cleanup stale_jobs_processed=%d", cleaned)

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
