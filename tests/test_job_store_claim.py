"""
Regression test for the vast.ai / WORKER_BACKEND_MODE=api claim bug (P0-1):

The GPU dispatcher moves a job to status='gpu_requested' (mark_gpu_requested)
right after requesting an ephemeral instance -- well before that instance
boots and its worker calls POST /api/worker/claim -> claim_specific_job.
Previously claim_specific_job only matched status='queued', so the claim
always found 0 rows and the job got stuck until the startup-SLA timeout
destroyed the (innocent) instance.

No real Postgres is used here (none of the existing tests use one either --
see tests/conftest.py and tests/test_auth.py). claim_specific_job's
DB access is exercised through a minimal fake connection/cursor that
actually evaluates the UPDATE ... WHERE ... RETURNING * predicate against
an in-memory row, so the test fails if the WHERE clause regresses to only
accepting 'queued' again.
"""
from __future__ import annotations

from scripts import prod_job_store


class _FakeCursor:
    def __init__(self, rows: dict) -> None:
        self._rows = rows
        self._result = None

    def execute(self, sql: str, params: tuple) -> None:
        sql_norm = " ".join(sql.split())
        assert sql_norm.startswith("UPDATE generation_jobs"), f"unexpected SQL: {sql_norm[:120]}"
        assert "RETURNING *" in sql_norm

        new_status, worker_id, claimed_at, started_at, worker_started_at, updated_at, \
            job_id, claimable_statuses = params

        row = self._rows.get(job_id)
        if row is not None and row["status"] in claimable_statuses:
            row["status"] = new_status
            row["worker_id"] = worker_id
            row["claimed_at"] = claimed_at
            row["started_at"] = started_at
            row["worker_started_at"] = worker_started_at
            row["updated_at"] = updated_at
            self._result = dict(row)
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self) -> None:
        pass


def _job_row(status: str) -> dict:
    return {
        "id": "job-1",
        "status": status,
        "worker_id": None,
        "claimed_at": None,
        "started_at": None,
        "worker_started_at": None,
        "updated_at": None,
    }


def test_claim_specific_job_claims_gpu_requested_job(monkeypatch):
    """queued -> gpu_requested -> worker claim -> claimed"""
    rows = {"job-1": _job_row("gpu_requested")}
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeConn(rows))

    claimed = prod_job_store.claim_specific_job("job-1", worker_id="gpu-worker-1")

    assert claimed is not None
    assert claimed["status"] == prod_job_store.JOB_STATUS_CLAIMED
    assert claimed["worker_id"] == "gpu-worker-1"
    assert claimed["worker_started_at"] is not None

    # Persisted, not just returned.
    assert rows["job-1"]["status"] == prod_job_store.JOB_STATUS_CLAIMED
    assert rows["job-1"]["worker_started_at"] is not None


def test_claim_specific_job_still_claims_plain_queued_job(monkeypatch):
    """Direct DB-mode path (no dispatcher involved) must keep working."""
    rows = {"job-1": _job_row("queued")}
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeConn(rows))

    claimed = prod_job_store.claim_specific_job("job-1", worker_id="w1")

    assert claimed is not None
    assert claimed["status"] == prod_job_store.JOB_STATUS_CLAIMED


def test_claim_specific_job_rejects_already_claimed_job(monkeypatch):
    """A job already claimed (or completed/failed/etc.) must not be re-claimable."""
    rows = {"job-1": _job_row("claimed")}
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeConn(rows))

    claimed = prod_job_store.claim_specific_job("job-1", worker_id="w2")

    assert claimed is None
    assert rows["job-1"]["status"] == "claimed"  # untouched
