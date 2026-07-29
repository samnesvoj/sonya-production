"""
Regression / spec tests for the future automatic Vast.ai instance lifecycle
(P0-4):

    job -> request GPU -> create Vast instance -> run worker image
    -> download models -> run job -> upload result -> explicit destroy

Today's production GPU is a single, manually-provisioned, persistently
running worker -- NOT the automatic dispatcher. This lifecycle must stay a
no-op for it:

  - prod_job_store.get_ephemeral_contract_id(job) is the single source of
    truth for "is this job's GPU instance safe to destroy automatically".
    It only returns a contract_id when the job is terminal AND its
    orchestrator_payload carries gpu_managed_by="vast_ephemeral" -- a stamp
    written ONLY by gpu_orchestrator._trigger_vast() when the automatic
    dispatcher provisions a per-job instance. A manually-managed worker
    never calls mark_gpu_requested(), so its jobs never carry that stamp.

  - gpu_orchestrator.cleanup_instance_for_terminal_job(job) combines that
    check with the actual (mocked, here) Vast destroy call.

  - scripts.prod_generation_api wires this into the worker /complete and
    /fail endpoints as a FastAPI background task, so a destroy failure can
    never flip a completed job back to failed.

No real Vast API call is ever made in these tests -- destroy_vast_instance
is monkeypatched everywhere it could be invoked.
"""
from __future__ import annotations

import os

from scripts import gpu_orchestrator, prod_job_store

WORKER_SECRET = os.environ["WORKER_SECRET"]
AUTH_HEADER = {"Authorization": f"Bearer {WORKER_SECRET}"}


def _job(status: str, orchestrator_payload=None, job_id: str = "job-1") -> dict:
    return {
        "id": job_id,
        "user_id": "u1",
        "status": status,
        "orchestrator_payload": orchestrator_payload,
    }


def _vast_ephemeral_payload(contract_id: str = "contract-42") -> dict:
    return {"ok": True, "provider": "vast", "gpu_managed_by": "vast_ephemeral", "contract_id": contract_id}


# ── get_ephemeral_contract_id: pure ownership/terminal-state logic ──────────


def test_ephemeral_contract_id_for_terminal_vast_ephemeral_job():
    job = _job("completed", _vast_ephemeral_payload("contract-42"))
    assert prod_job_store.get_ephemeral_contract_id(job) == "contract-42"


def test_no_contract_id_for_manual_job():
    """orchestrator_payload absent entirely -- the manually-managed worker path."""
    job = _job("completed", orchestrator_payload=None)
    assert prod_job_store.get_ephemeral_contract_id(job) is None


def test_no_contract_id_for_explicit_manual_stamp():
    job = _job("completed", {"gpu_managed_by": "manual"})
    assert prod_job_store.get_ephemeral_contract_id(job) is None


def test_no_contract_id_for_unknown_ownership_value():
    job = _job("completed", {"gpu_managed_by": "something_else", "contract_id": "c1"})
    assert prod_job_store.get_ephemeral_contract_id(job) is None


def test_no_contract_id_when_ownership_key_missing():
    job = _job("completed", {"contract_id": "c1"})  # no gpu_managed_by key at all
    assert prod_job_store.get_ephemeral_contract_id(job) is None


def test_no_contract_id_for_non_terminal_job():
    """Even a vast_ephemeral job must not be touched while still in flight."""
    job = _job("mode_running", _vast_ephemeral_payload())
    assert prod_job_store.get_ephemeral_contract_id(job) is None


def test_no_contract_id_when_payload_is_malformed_json_string():
    job = _job("completed", orchestrator_payload="not valid json")
    assert prod_job_store.get_ephemeral_contract_id(job) is None


# ── gpu_orchestrator.cleanup_instance_for_terminal_job ───────────────────────


def test_cleanup_destroys_vast_ephemeral_terminal_instance(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    job = _job("completed", _vast_ephemeral_payload("contract-42"))
    result = gpu_orchestrator.cleanup_instance_for_terminal_job(job)

    assert result is True
    assert calls == ["contract-42"]


def test_cleanup_does_not_destroy_manual_instance(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    job = _job("completed", orchestrator_payload=None)
    result = gpu_orchestrator.cleanup_instance_for_terminal_job(job)

    assert result is False
    assert calls == []


def test_cleanup_safely_skips_unknown_ownership(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    job = _job("failed", {"gpu_managed_by": "??", "contract_id": "c1"})
    result = gpu_orchestrator.cleanup_instance_for_terminal_job(job)

    assert result is False
    assert calls == []


def test_cleanup_skips_non_terminal_job(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    job = _job("uploading_result", _vast_ephemeral_payload())
    result = gpu_orchestrator.cleanup_instance_for_terminal_job(job)

    assert result is False
    assert calls == []


def test_repeated_cleanup_of_same_ephemeral_instance_is_safe(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    job = _job("completed", _vast_ephemeral_payload("contract-42"))

    first = gpu_orchestrator.cleanup_instance_for_terminal_job(job)
    second = gpu_orchestrator.cleanup_instance_for_terminal_job(job)

    assert first is True
    assert second is True
    assert calls == ["contract-42", "contract-42"]


# ── End-to-end through the worker /complete and /fail endpoints ─────────────


def test_complete_endpoint_triggers_destroy_for_vast_ephemeral_job(client, monkeypatch):
    monkeypatch.setattr("scripts.prod_generation_api.complete_job", lambda **kw: None)
    monkeypatch.setattr(
        "scripts.prod_generation_api.get_job",
        lambda job_id: _job("completed", _vast_ephemeral_payload("contract-99"), job_id),
    )
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    resp = client.post(
        "/api/worker/jobs/job-1/complete",
        json={"s3_output_key": "out/key.mp4", "clip_count": 1, "processing_ms": 100},
        headers=AUTH_HEADER,
    )

    assert resp.status_code == 200
    assert calls == ["contract-99"]


def test_complete_endpoint_does_not_destroy_manually_managed_gpu(client, monkeypatch):
    monkeypatch.setattr("scripts.prod_generation_api.complete_job", lambda **kw: None)
    monkeypatch.setattr(
        "scripts.prod_generation_api.get_job",
        lambda job_id: _job("completed", orchestrator_payload=None, job_id=job_id),
    )
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    resp = client.post(
        "/api/worker/jobs/job-1/complete",
        json={"s3_output_key": "out/key.mp4"},
        headers=AUTH_HEADER,
    )

    assert resp.status_code == 200
    assert calls == []


def test_vast_api_error_on_complete_does_not_flip_job_to_failed(client, monkeypatch):
    """
    destroy_vast_instance raising (or returning False) after a successful
    /complete must never turn the completed job into a failed one -- the
    cleanup call happens strictly after complete_job() already committed
    status=completed, and any error in it must be swallowed.
    """
    fail_calls = []
    monkeypatch.setattr("scripts.prod_generation_api.complete_job", lambda **kw: None)
    monkeypatch.setattr("scripts.prod_generation_api.fail_job", lambda **kw: fail_calls.append(kw))
    monkeypatch.setattr(
        "scripts.prod_generation_api.get_job",
        lambda job_id: _job("completed", _vast_ephemeral_payload("contract-99"), job_id),
    )

    def _boom(contract_id):
        raise RuntimeError("vast.ai API unreachable")

    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", _boom)

    resp = client.post(
        "/api/worker/jobs/job-1/complete",
        json={"s3_output_key": "out/key.mp4"},
        headers=AUTH_HEADER,
    )

    # Endpoint itself must not 500, and must never have tried to fail the job.
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "job_id": "job-1"}
    assert fail_calls == []


def test_fail_endpoint_does_not_destroy_when_job_requeued_for_retry(client, monkeypatch):
    """fail_job(retry=True) with retries left requeues -> status='queued', non-terminal."""
    monkeypatch.setattr("scripts.prod_generation_api.fail_job", lambda **kw: None)
    monkeypatch.setattr(
        "scripts.prod_generation_api.get_job",
        lambda job_id: _job("queued", _vast_ephemeral_payload(), job_id),
    )
    calls = []
    monkeypatch.setattr(gpu_orchestrator, "destroy_vast_instance", lambda cid: calls.append(cid) or True)

    resp = client.post(
        "/api/worker/jobs/job-1/fail",
        json={"error_code": "transient", "error_message": "network blip", "retry": True},
        headers=AUTH_HEADER,
    )

    assert resp.status_code == 200
    assert calls == []
