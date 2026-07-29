"""
Regression test for the GPU concurrency cap bug (P0):

count_active_gpu_jobs() is the sole gate the dispatcher uses to enforce
MAX_ACTIVE_GPU_JOBS (default 1 -- one paid ephemeral vast.ai instance at a
time). It used to count a hand-rolled subset of statuses
("gpu_requested", "gpu_booting", "worker_started", "model_downloading")
instead of the canonical _ACTIVE_STATUSES tuple, so a job silently stopped
counting as active as soon as it moved past model_downloading -- exactly
the states (mode_running, analyzing, yolo, scripting, tts, subtitles,
assembling, uploading_result) where the GPU instance is actually doing
the real, longest-running work. The dispatcher would then believe
capacity was free and fire a second paid instance while the first was
still running.

No real Postgres is used here (see tests/test_job_store_claim.py for the
same pattern) -- the fake cursor evaluates the real
`WHERE status = ANY(%s)` predicate against an in-memory set of jobs, so
the test fails if the status list regresses to a narrower subset again.
"""
from __future__ import annotations

from scripts import prod_job_store


class _FakeCursor:
    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses
        self._result = None

    def execute(self, sql: str, params: tuple) -> None:
        sql_norm = " ".join(sql.split())
        assert sql_norm.startswith("SELECT COUNT(*) AS n FROM generation_jobs")
        assert "status = ANY(%s)" in sql_norm

        (claimable_statuses,) = params
        n = sum(1 for s in self._statuses if s in claimable_statuses)
        self._result = {"n": n}

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._statuses)

    def close(self) -> None:
        pass


def _count_with_statuses(monkeypatch, statuses: list[str]) -> int:
    monkeypatch.setattr(prod_job_store, "_get_conn", lambda: _FakeConn(statuses))
    return prod_job_store.count_active_gpu_jobs()


def test_counts_job_in_mode_running(monkeypatch):
    assert _count_with_statuses(monkeypatch, ["mode_running"]) == 1


def test_counts_job_in_tts(monkeypatch):
    assert _count_with_statuses(monkeypatch, ["tts"]) == 1


def test_counts_job_in_uploading_result(monkeypatch):
    assert _count_with_statuses(monkeypatch, ["uploading_result"]) == 1


def test_counts_job_in_claimed(monkeypatch):
    assert _count_with_statuses(monkeypatch, ["claimed"]) == 1


def test_does_not_count_queued_or_terminal_jobs(monkeypatch):
    statuses = ["queued", "completed", "failed", "cancelled"]
    assert _count_with_statuses(monkeypatch, statuses) == 0


def test_counts_multiple_concurrent_active_jobs_across_lifecycle(monkeypatch):
    statuses = ["gpu_requested", "mode_running", "tts", "uploading_result", "queued"]
    assert _count_with_statuses(monkeypatch, statuses) == 4
