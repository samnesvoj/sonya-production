"""
Worker endpoints must remain unaffected by the new cookie-session auth:
still Bearer WORKER_SECRET only, no cookie/session involved at all.
"""
from __future__ import annotations

import os


WORKER_SECRET = os.environ["WORKER_SECRET"]
AUTH_HEADER = {"Authorization": f"Bearer {WORKER_SECRET}"}


def test_worker_claim_requires_bearer_secret(client):
    resp = client.post("/api/worker/claim", json={"worker_id": "w1"})
    assert resp.status_code == 403


def test_worker_claim_rejects_wrong_secret(client):
    resp = client.post(
        "/api/worker/claim",
        json={"worker_id": "w1"},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 403


def test_worker_claim_works_with_correct_secret(client, monkeypatch):
    monkeypatch.setattr(
        "scripts.prod_generation_api.claim_next_pending_job",
        lambda worker_id, modes=None: {"id": "job-1", "mode": "virality", "user_id": "u1"},
    )
    resp = client.post("/api/worker/claim", json={"worker_id": "w1"}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["job"]["id"] == "job-1"


def test_worker_claim_specific_job(client, monkeypatch):
    captured = {}

    def fake_claim_specific(job_id, worker_id):
        captured["job_id"] = job_id
        captured["worker_id"] = worker_id
        return {"id": job_id, "mode": "virality", "user_id": "u1"}

    monkeypatch.setattr("scripts.prod_generation_api.claim_specific_job", fake_claim_specific)
    resp = client.post(
        "/api/worker/claim",
        json={"worker_id": "w1", "job_id": "job-42"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    assert captured == {"job_id": "job-42", "worker_id": "w1"}


def test_worker_status_update_requires_secret(client):
    resp = client.post("/api/worker/jobs/job-1/status", json={"status": "downloading"})
    assert resp.status_code == 403


def test_worker_status_update_works_with_secret(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scripts.prod_generation_api.update_job_status",
        lambda job_id, status: calls.append((job_id, status)),
    )
    resp = client.post(
        "/api/worker/jobs/job-1/status",
        json={"status": "downloading"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    assert calls == [("job-1", "downloading")]


def test_worker_complete_job_works_with_secret(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scripts.prod_generation_api.complete_job",
        lambda **kw: calls.append(kw),
    )
    monkeypatch.setattr(
        "scripts.prod_generation_api.get_job",
        lambda job_id: {"id": job_id, "user_id": "u1"},
    )
    resp = client.post(
        "/api/worker/jobs/job-1/complete",
        json={"s3_output_key": "out/key.mp4", "clip_count": 3, "processing_ms": 1000},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "job_id": "job-1"}
    assert calls[0]["s3_output_key"] == "out/key.mp4"


def test_worker_complete_job_requires_secret(client):
    resp = client.post(
        "/api/worker/jobs/job-1/complete",
        json={"s3_output_key": "out/key.mp4"},
    )
    assert resp.status_code == 403
