"""
Durability tests for the vast.ai ephemeral-instance destroy step (P0-4
follow-up).

The immediate FastAPI BackgroundTask "fast path" (see
prod_generation_api._cleanup_ephemeral_instance) is only a best-effort
in-process callback: if the API process crashes or restarts between
committing a job's terminal status and running that task, the instance is
never destroyed and nothing retries it from that side. The same gap exists
after any destroy attempt that simply failed (Vast API error).

gpu_dispatcher.reconcile_gpu_instance_cleanup() is the durable retry path:
it re-derives candidates straight from Postgres on every tick via
prod_job_store.get_terminal_jobs_pending_gpu_cleanup() and calls the exact
same idempotent gpu_orchestrator.cleanup_instance_for_terminal_job() the
fast path uses. Cleanup confirmation is stored in the existing
orchestrator_payload JSONB column (gpu_cleanup_status) -- no schema
migration -- so a confirmed destroy is never re-attempted on a later tick.

No real Vast API call is made anywhere in this file -- destroy_vast_instance
is monkeypatched (or, for the 404 test, a fake requests.delete stands in for
the real HTTP call).
"""
from __future__ import annotations

import scripts.gpu_dispatcher as gpu_dispatcher
import scripts.gpu_orchestrator as gpu_orchestrator
from scripts import prod_job_store


def _job(job_id: str, status: str, orchestrator_payload=None) -> dict:
    return {"id": job_id, "status": status, "orchestrator_payload": orchestrator_payload}


def _vast_ephemeral_payload(contract_id: str = "contract-1", **extra) -> dict:
    return {"gpu_managed_by": "vast_ephemeral", "contract_id": contract_id, **extra}


# ── get_terminal_jobs_pending_gpu_cleanup: real SQL filter, fake cursor ─────
# Same pattern as tests/test_job_store_claim.py: no real Postgres, but the
# fake cursor evaluates the actual WHERE-clause semantics against an
# in-memory table, so the test fails if the filter regresses.


class _FakeCleanupCursor:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self._result: list = []

    def execute(self, sql: str, params: tuple) -> None:
        sql_norm = " ".join(sql.split())
        assert sql_norm.startswith("SELECT * FROM generation_jobs")
        assert "orchestrator_payload->>'gpu_managed_by' = %s" in sql_norm
        assert "orchestrator_payload->>'contract_id' IS NOT NULL" in sql_norm
        assert "orchestrator_payload->>'gpu_cleanup_status'" in sql_norm

        terminal_statuses, managed_by, destroyed_status = params
        matched = []
        for row in self._rows:
            if row["status"] not in terminal_statuses:
                continue
            payload = row.get("orchestrator_payload") or {}
            if payload.get("gpu_managed_by") != managed_by:
                continue
            if not payload.get("contract_id"):
                continue
            if (payload.get("gpu_cleanup_status") or "") == destroyed_status:
                continue
            matched.append(row)
        self._result = matched

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeCleanupConn:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCleanupCursor:
        return _FakeCleanupCursor(self._rows)

    def close(self) -> None:
        pass


def test_query_finds_eligible_vast_ephemeral_terminal_job(monkeypatch):
    rows = [_job("job-1", "completed", _vast_ephemeral_payload())]
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeCleanupConn(rows))
    result = prod_job_store.get_terminal_jobs_pending_gpu_cleanup()
    assert [r["id"] for r in result] == ["job-1"]


def test_query_excludes_manual_job_with_no_payload(monkeypatch):
    rows = [_job("job-1", "completed", None)]
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeCleanupConn(rows))
    assert prod_job_store.get_terminal_jobs_pending_gpu_cleanup() == []


def test_query_excludes_explicit_manual_ownership(monkeypatch):
    rows = [_job("job-1", "completed", {"gpu_managed_by": "manual", "contract_id": "c1"})]
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeCleanupConn(rows))
    assert prod_job_store.get_terminal_jobs_pending_gpu_cleanup() == []


def test_query_excludes_missing_ownership_key(monkeypatch):
    rows = [_job("job-1", "completed", {"contract_id": "c1"})]  # no gpu_managed_by at all
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeCleanupConn(rows))
    assert prod_job_store.get_terminal_jobs_pending_gpu_cleanup() == []


def test_query_excludes_non_terminal_job(monkeypatch):
    rows = [_job("job-1", "mode_running", _vast_ephemeral_payload())]
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeCleanupConn(rows))
    assert prod_job_store.get_terminal_jobs_pending_gpu_cleanup() == []


def test_query_excludes_already_confirmed_destroyed(monkeypatch):
    rows = [_job("job-1", "completed", _vast_ephemeral_payload(gpu_cleanup_status="destroyed"))]
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeCleanupConn(rows))
    assert prod_job_store.get_terminal_jobs_pending_gpu_cleanup() == []


# ── reconcile_gpu_instance_cleanup: multi-tick behavior, in-memory job store ─


class _FakeGpuJobStore:
    """
    In-memory stand-in for the terminal/vast_ephemeral slice of
    prod_job_store, wired into gpu_dispatcher + gpu_orchestrator via
    monkeypatch. query_pending() mirrors get_terminal_jobs_pending_gpu_
    cleanup()'s real filter; mark_destroyed/mark_error mirror the real
    mark_gpu_instance_cleanup_destroyed/_error() effect of mutating
    orchestrator_payload -- without touching Postgres.
    """

    def __init__(self, jobs: dict) -> None:
        self.jobs = jobs  # job_id -> job dict (mutated in place)

    def query_pending(self) -> list:
        pending = []
        for job in self.jobs.values():
            if job["status"] not in prod_job_store._TERMINAL_STATUSES:
                continue
            payload = job.get("orchestrator_payload") or {}
            if payload.get("gpu_managed_by") != prod_job_store.GPU_MANAGED_BY_VAST_EPHEMERAL:
                continue
            if not payload.get("contract_id"):
                continue
            if payload.get("gpu_cleanup_status") == prod_job_store.GPU_CLEANUP_STATUS_DESTROYED:
                continue
            pending.append(job)
        return pending

    def mark_destroyed(self, job_id: str) -> None:
        self.jobs[job_id]["orchestrator_payload"]["gpu_cleanup_status"] = "destroyed"

    def mark_error(self, job_id: str, error: str) -> None:
        self.jobs[job_id]["orchestrator_payload"]["gpu_cleanup_status"] = "error"
        self.jobs[job_id]["orchestrator_payload"]["gpu_cleanup_error"] = error


def _wire_fake_store(monkeypatch, store: _FakeGpuJobStore) -> None:
    monkeypatch.setattr(gpu_dispatcher, "get_terminal_jobs_pending_gpu_cleanup", store.query_pending)
    monkeypatch.setattr(gpu_orchestrator, "mark_gpu_instance_cleanup_destroyed", store.mark_destroyed)
    monkeypatch.setattr(gpu_orchestrator, "mark_gpu_instance_cleanup_error", store.mark_error)


def test_reconciliation_destroys_instance_when_background_task_never_ran(monkeypatch):
    """Terminal job persisted (as complete_job()/fail_job() would leave it),
    but the FastAPI BackgroundTask never ran -- the dispatcher tick must
    still destroy it."""
    jobs = {"job-1": _job("job-1", "completed", _vast_ephemeral_payload("contract-1"))}
    store = _FakeGpuJobStore(jobs)
    _wire_fake_store(monkeypatch, store)
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    processed = gpu_dispatcher.reconcile_gpu_instance_cleanup()

    assert processed == 1
    assert calls == ["contract-1"]
    assert jobs["job-1"]["orchestrator_payload"]["gpu_cleanup_status"] == "destroyed"


def test_failed_destroy_attempt_is_retried_on_next_tick(monkeypatch):
    jobs = {"job-1": _job("job-1", "failed", _vast_ephemeral_payload("contract-1"))}
    store = _FakeGpuJobStore(jobs)
    _wire_fake_store(monkeypatch, store)

    outcomes = iter([False, True])
    calls = []

    def fake_destroy(contract_id):
        calls.append(contract_id)
        return next(outcomes)

    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", fake_destroy)

    first_tick = gpu_dispatcher.reconcile_gpu_instance_cleanup()
    assert first_tick == 1
    assert jobs["job-1"]["orchestrator_payload"]["gpu_cleanup_status"] == "error"

    second_tick = gpu_dispatcher.reconcile_gpu_instance_cleanup()
    assert second_tick == 1
    assert jobs["job-1"]["orchestrator_payload"]["gpu_cleanup_status"] == "destroyed"

    assert calls == ["contract-1", "contract-1"]


def test_confirmed_destroy_is_not_repeated_on_later_ticks(monkeypatch):
    jobs = {"job-1": _job("job-1", "completed", _vast_ephemeral_payload("contract-1"))}
    store = _FakeGpuJobStore(jobs)
    _wire_fake_store(monkeypatch, store)
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    first_tick = gpu_dispatcher.reconcile_gpu_instance_cleanup()
    second_tick = gpu_dispatcher.reconcile_gpu_instance_cleanup()
    third_tick = gpu_dispatcher.reconcile_gpu_instance_cleanup()

    assert first_tick == 1
    assert second_tick == 0
    assert third_tick == 0
    assert calls == ["contract-1"]  # Vast API hit exactly once, ever.


def test_reconciliation_ignores_manual_missing_and_non_terminal_jobs(monkeypatch):
    jobs = {
        "manual-no-payload": _job("manual-no-payload", "completed", None),
        "manual-explicit": _job("manual-explicit", "completed", {"gpu_managed_by": "manual", "contract_id": "c2"}),
        "unknown-ownership": _job("unknown-ownership", "failed", {"contract_id": "c3"}),
        "still-running": _job("still-running", "mode_running", _vast_ephemeral_payload("c4")),
    }
    store = _FakeGpuJobStore(jobs)
    _wire_fake_store(monkeypatch, store)
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    processed = gpu_dispatcher.reconcile_gpu_instance_cleanup()

    assert processed == 0
    assert calls == []
    # None of the manual/unknown/non-terminal jobs got a cleanup stamp either.
    assert jobs["manual-explicit"]["orchestrator_payload"].get("gpu_cleanup_status") is None
    assert jobs["unknown-ownership"]["orchestrator_payload"].get("gpu_cleanup_status") is None
    assert jobs["still-running"]["orchestrator_payload"].get("gpu_cleanup_status") is None


# ── 404 / already-destroyed handling ─────────────────────────────────────────


def test_destroy_vast_instance_treats_404_as_success(monkeypatch):
    monkeypatch.setattr(gpu_orchestrator, "_VAST_API_KEY", "test-key")

    class _FakeResp:
        ok = False
        status_code = 404
        text = "instance not found"

    import requests

    monkeypatch.setattr(requests, "delete", lambda *a, **kw: _FakeResp())

    assert gpu_orchestrator.destroy_vast_instance("contract-1") is True


def test_404_cleanup_is_recorded_as_confirmed_destroyed(monkeypatch):
    """destroy_vast_instance already normalizes 404 -> True; verify that a
    True outcome (whatever HTTP status produced it) is what gets persisted
    as the durable "confirmed destroyed" marker."""
    calls = []
    monkeypatch.setattr(
        gpu_orchestrator, "mark_gpu_instance_cleanup_destroyed",
        lambda job_id: calls.append(("destroyed", job_id)),
    )
    monkeypatch.setattr(
        gpu_orchestrator, "mark_gpu_instance_cleanup_error",
        lambda job_id, error: calls.append(("error", job_id, error)),
    )
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda contract_id: True)

    job = _job("job-1", "completed", _vast_ephemeral_payload("contract-1"))
    result = gpu_orchestrator.cleanup_instance_for_terminal_job(job)

    assert result is True
    assert calls == [("destroyed", "job-1")]
