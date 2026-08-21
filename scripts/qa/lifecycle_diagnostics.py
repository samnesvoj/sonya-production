"""
lifecycle_diagnostics.py
=========================
Stage-by-stage diagnostics for the SONYA generation pipeline:

  FILE_SELECTED -> CLIENT_UPLOAD_START -> CLIENT_UPLOAD_COMPLETE ->
  BACKEND_REQUEST_RECEIVED -> INPUT_VALIDATED -> S3_INPUT_UPLOAD ->
  JOB_CREATED -> QUEUED -> WORKER_CLAIMED -> PROCESSING ->
  OUTPUT_UPLOADED -> JOB_COMPLETED -> FRONTEND_RESULT_RECEIVED

Root cause this exists to make visible (see docs/QA_GENERATION_LIFECYCLE.md):
POST /api/generation/jobs is ONE multipart request that bundles the browser
upload, backend receipt, upload validation, the synchronous S3 PUT, and the
Postgres job insert. From outside that single request/response, none of
those sub-stages are independently observable -- "СОЗДАЁМ ЗАДАЧУ" is one
opaque span covering all of them, with no client-side timeout and no
server-side per-stage timeout either.

This module can't change that wire contract, but in *mock* mode (the
FastAPI TestClient with the S3/DB boundary functions monkeypatched) it CAN
recover per-stage timing by timestamping each mocked call as it happens --
`probe()` below wraps a boundary function so calling it records a
monotonic timestamp before delegating to the real implementation. That's
how run_backend_lifecycle() below produces INPUT_VALIDATED / S3_INPUT_UPLOAD
/ JOB_CREATED as distinct, timed stages even though the real HTTP wire
protocol cannot.

In *live* mode (a real base_url, no mocking possible) those three collapse
into a single BACKEND_PROCESSING span, exactly mirroring what a real
browser observes today -- itself a diagnostic finding, not a limitation to
hide.
"""
from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# Minimal bytes that pass upload_security's magic-byte sniff for mp4 (mirrors
# tests/test_generation_jobs_auth.py::_mp4_bytes).
DEFAULT_VIDEO_BYTES = b"\x00\x00\x00\x18ftyp" + b"\x00" * 64

STAGE_ORDER = [
    "CLIENT_UPLOAD_START",
    "CLIENT_UPLOAD_COMPLETE",
    "BACKEND_REQUEST_RECEIVED",
    "INPUT_VALIDATED",
    "S3_INPUT_UPLOAD",
    "JOB_CREATED",
    "QUEUED",
    "WORKER_CLAIMED",
    "PROCESSING",
    "OUTPUT_UPLOADED",
    "JOB_COMPLETED",
    "FRONTEND_RESULT_RECEIVED",
]


@dataclass
class StageResult:
    stage: str
    ok: bool
    duration_s: Optional[float] = None
    detail: str = ""
    trace_id: Optional[str] = None
    timed_out: bool = False


def format_stage(r: StageResult) -> str:
    status = "TIMEOUT" if r.timed_out else ("PASS" if r.ok else "FAIL")
    line = f"{status.ljust(7)} {r.stage.ljust(26)}"
    if r.duration_s is not None:
        line += f"{r.duration_s:6.1f}s"
    if r.detail:
        line += f"  {r.detail}"
    out = [line]
    if not r.ok and r.trace_id:
        out.append(f"        trace_id={r.trace_id}")
    return "\n".join(out)


def format_report(results: List[StageResult]) -> str:
    return "\n".join(format_stage(r) for r in results)


def all_passed(results: List[StageResult]) -> bool:
    return all(r.ok for r in results)


def _err_detail(resp) -> str:
    try:
        data = resp.json()
    except Exception:
        return f"body={resp.text[:200]!r}"
    detail = data.get("detail", data)
    return f"reason={detail}"


def _pad_not_reached(results: List[StageResult]) -> None:
    seen = {r.stage for r in results}
    for stage in STAGE_ORDER:
        if stage not in seen:
            results.append(StageResult(stage, False, detail="not reached"))


def probe(probes: Dict[str, float], key: str, fn: Callable, *, delay_s: float = 0.0,
          raise_exc: Optional[BaseException] = None) -> Callable:
    """Wrap a boundary function (upload_bytes, create_job_idempotent, ...) so
    calling it records a monotonic timestamp under `probes[key]` before
    delegating to `fn`. `delay_s` simulates a stalled/slow dependency;
    `raise_exc` simulates that dependency failing outright (S3 down, DB
    down) -- both without needing a real S3 bucket or Postgres instance."""

    def wrapped(*args: Any, **kwargs: Any):
        probes[key] = time.monotonic()
        if delay_s:
            time.sleep(delay_s)
        if raise_exc is not None:
            raise raise_exc
        return fn(*args, **kwargs)

    return wrapped


def probe_async(probes: Dict[str, float], key: str, fn: Callable, *, delay_s: float = 0.0,
                 raise_exc: Optional[BaseException] = None) -> Callable:
    """Same as probe(), for an `async def` boundary function (validate_upload
    is awaited by the real endpoint)."""

    async def wrapped(*args: Any, **kwargs: Any):
        probes[key] = time.monotonic()
        if delay_s:
            import asyncio
            await asyncio.sleep(delay_s)
        if raise_exc is not None:
            raise raise_exc
        return await fn(*args, **kwargs)

    return wrapped


def _call_with_deadline(fn: Callable[[], Any], timeout_s: float):
    """Run fn() on a daemon thread and enforce a real wall-clock deadline.

    Why not just pass timeout=... to the HTTP client? TestClient runs the
    ASGI app in-process, synchronously, on the calling thread -- a blocking
    call inside a mocked dependency (e.g. upload_bytes doing time.sleep(...)
    to simulate a stalled S3) blocks that same thread, and httpx's
    client-side timeout has no way to preempt code running in its own call
    stack. A real stalled TCP connection in --mode live *would* be
    preemptable by httpx's timeout; this daemon-thread deadline makes
    --mode mock behave the same way for QA purposes -- report TIMEOUT and
    move on, never hang the diagnostic run itself. The stalled call's
    thread is left running in the background (daemon=True), which is fine:
    it dies with the process instead of blocking it.

    Returns (result, exc, timed_out).
    """
    result: List[Any] = [None]
    exc: List[Optional[BaseException]] = [None]
    done = threading.Event()

    def target():
        try:
            result[0] = fn()
        except BaseException as e:  # noqa: BLE001 -- must capture to report it, not swallow it
            exc[0] = e
        finally:
            done.set()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    if not done.wait(timeout_s):
        return None, None, True
    if exc[0] is not None:
        return None, exc[0], False
    return result[0], None, False


def run_backend_lifecycle(
    client,
    *,
    mode: str = "virality",
    video_bytes: bytes = DEFAULT_VIDEO_BYTES,
    idempotency_key: Optional[str] = None,
    stage_timeout_s: float = 5.0,
    probes: Optional[Dict[str, float]] = None,
    simulate_worker: bool = True,
    worker_secret: Optional[str] = None,
    worker_id: str = "qa-diagnostic-worker",
) -> List[StageResult]:
    """Drives POST /api/generation/jobs through a TestClient (or any
    requests/httpx-compatible client with the same call signature) exactly
    as the browser's apiCreateVideoJob() does, then -- if the job was
    created -- polls GET /api/generation/jobs/{id} and, when
    simulate_worker is True, walks the job through the real
    /api/worker/claim -> /status -> /complete endpoints (never a real
    vast.ai instance) before reading back /result-url. Returns one
    StageResult per stage in STAGE_ORDER; a failure at any stage still
    returns a full list with the remaining stages marked "not reached", so
    a caller can print the whole report and see exactly where it broke.
    """
    results: List[StageResult] = []
    probes = probes if probes is not None else {}
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}

    t0 = time.monotonic()
    resp, exc, timed_out = _call_with_deadline(
        lambda: client.post(
            "/api/generation/jobs",
            data={"mode": mode},
            files={"file": ("clip.mp4", io.BytesIO(video_bytes), "video/mp4")},
            headers=headers,
        ),
        stage_timeout_s,
    )
    dt = time.monotonic() - t0

    if timed_out or exc is not None:
        detail = (f"no response within {stage_timeout_s:.1f}s -- request never settled"
                   if timed_out else f"reason={exc!r}")
        results.append(StageResult("CLIENT_UPLOAD_START", True, 0.0, "request sent"))
        results.append(StageResult("CLIENT_UPLOAD_COMPLETE", False, dt, detail=detail, timed_out=timed_out))
        _pad_not_reached(results)
        return results

    # apiCreateVideoJob() in auth.js sends this as ONE fetch() -- start and
    # complete of the browser's own byte upload are not independently
    # observable from here (or from the browser itself: fetch() has no
    # upload-progress event). See docs/QA_GENERATION_LIFECYCLE.md.
    results.append(StageResult("CLIENT_UPLOAD_START", True, 0.0, "request sent"))
    results.append(StageResult("CLIENT_UPLOAD_COMPLETE", True, dt, "full request/response round trip"))

    validated_at = probes.get("validated_at")
    s3_start = probes.get("s3_upload_start")
    s3_done = probes.get("s3_upload_done")

    if resp.status_code >= 500:
        results.append(StageResult("BACKEND_REQUEST_RECEIVED", True))
        if not validated_at:
            # 500 before validate_upload was even called -- e.g. priority
            # resolution or param parsing blew up.
            results.append(StageResult("INPUT_VALIDATED", False, detail=f"status={resp.status_code} {_err_detail(resp)}"))
            _pad_not_reached(results)
            return results
        results.append(StageResult("INPUT_VALIDATED", True, (validated_at - t0) or None))
        if not s3_start:
            # validate_upload ran and didn't raise (it only raises 400s) --
            # a 500 here with no S3 attempt recorded is between validation
            # and the S3 call (e.g. build_input_key/job_id generation).
            results.append(StageResult("S3_INPUT_UPLOAD", False, detail=f"status={resp.status_code} {_err_detail(resp)} (never attempted)"))
            _pad_not_reached(results)
            return results
        if not s3_done:
            results.append(StageResult("S3_INPUT_UPLOAD", False, (time.monotonic() - s3_start),
                                        detail=f"status={resp.status_code} {_err_detail(resp)}"))
            _pad_not_reached(results)
            return results
        results.append(StageResult("S3_INPUT_UPLOAD", True, s3_done - s3_start))
        results.append(StageResult("JOB_CREATED", False, detail=f"status={resp.status_code} {_err_detail(resp)} (DB insert likely failed)"))
        _pad_not_reached(results)
        return results

    if resp.status_code in (401, 402, 403, 429):
        results.append(StageResult("BACKEND_REQUEST_RECEIVED", False,
                                    detail=f"rejected before validation status={resp.status_code} {_err_detail(resp)}"))
        _pad_not_reached(results)
        return results

    if resp.status_code == 400:
        # invalid_mode / invalid_params / a malformed Idempotency-Key are
        # all rejected before validate_upload ever runs (see
        # create_generation_job() in prod_generation_api.py) -- distinguish
        # that from an actual upload_security rejection (bad magic bytes,
        # oversized file, disallowed extension) using the probe: it's only
        # set once validate_upload is actually called.
        if not validated_at:
            results.append(StageResult("BACKEND_REQUEST_RECEIVED", False,
                                        detail=f"request rejected before upload validation, status=400 {_err_detail(resp)}"))
        else:
            results.append(StageResult("BACKEND_REQUEST_RECEIVED", True))
            results.append(StageResult("INPUT_VALIDATED", False, detail=f"status=400 {_err_detail(resp)}"))
        _pad_not_reached(results)
        return results

    if resp.status_code not in (200, 201, 202):
        results.append(StageResult("BACKEND_REQUEST_RECEIVED", True))
        results.append(StageResult("JOB_CREATED", False, detail=f"unexpected status={resp.status_code} {_err_detail(resp)}"))
        _pad_not_reached(results)
        return results

    body = resp.json()
    job_id = body.get("job_id")

    results.append(StageResult("BACKEND_REQUEST_RECEIVED", True))

    validated_at = probes.get("validated_at")
    results.append(StageResult(
        "INPUT_VALIDATED", True,
        (validated_at - t0) if validated_at else None,
    ))

    s3_start = probes.get("s3_upload_start")
    s3_done = probes.get("s3_upload_done")
    results.append(StageResult(
        "S3_INPUT_UPLOAD", True,
        (s3_done - s3_start) if (s3_start and s3_done) else None,
    ))

    results.append(StageResult("JOB_CREATED", bool(job_id), None, f"job_id={job_id}"))
    if not job_id:
        _pad_not_reached(results)
        return results

    # QUEUED
    t_q = time.monotonic()
    st_resp = client.get(f"/api/generation/jobs/{job_id}", timeout=stage_timeout_s)
    st = (st_resp.json().get("status") if st_resp.status_code == 200 else None)
    results.append(StageResult("QUEUED", st_resp.status_code == 200 and st in ("queued", None),
                                time.monotonic() - t_q, f"status={st}"))

    if not simulate_worker or not worker_secret:
        _pad_not_reached(results)
        return results

    wh = {"Authorization": f"Bearer {worker_secret}"}

    # WORKER_CLAIMED
    t_c = time.monotonic()
    claim_resp = client.post("/api/worker/claim", json={"worker_id": worker_id, "job_id": job_id}, headers=wh)
    claimed_ok = claim_resp.status_code == 200 and (claim_resp.json().get("job") or {}).get("id") == job_id
    results.append(StageResult("WORKER_CLAIMED", claimed_ok, time.monotonic() - t_c,
                                f"status={claim_resp.status_code}"))
    if not claimed_ok:
        _pad_not_reached(results)
        return results

    # PROCESSING
    t_p = time.monotonic()
    proc_resp = client.post(f"/api/worker/jobs/{job_id}/status", json={"status": "mode_running"}, headers=wh)
    results.append(StageResult("PROCESSING", proc_resp.status_code == 200, time.monotonic() - t_p,
                                f"status={proc_resp.status_code}"))
    if proc_resp.status_code != 200:
        _pad_not_reached(results)
        return results

    # OUTPUT_UPLOADED + JOB_COMPLETED (worker reports both in one call, same
    # as production -- see /api/worker/jobs/{id}/complete)
    t_done = time.monotonic()
    complete_resp = client.post(
        f"/api/worker/jobs/{job_id}/complete",
        json={"s3_output_key": f"users/qa/jobs/{job_id}/{mode}/output/result.mp4", "clip_count": 1, "processing_ms": 1000},
        headers=wh,
    )
    complete_ok = complete_resp.status_code == 200
    dt_done = time.monotonic() - t_done
    results.append(StageResult("OUTPUT_UPLOADED", complete_ok, dt_done))
    results.append(StageResult("JOB_COMPLETED", complete_ok, None, f"status={complete_resp.status_code}"))
    if not complete_ok:
        _pad_not_reached(results)
        return results

    # FRONTEND_RESULT_RECEIVED -- GET .../result-url, exactly what
    # app.js::pollJob() does the instant it sees status === 'completed'.
    t_r = time.monotonic()
    result_resp = client.get(f"/api/generation/jobs/{job_id}/result-url", timeout=stage_timeout_s)
    result_ok = result_resp.status_code == 200 and bool(result_resp.json().get("url"))
    results.append(StageResult("FRONTEND_RESULT_RECEIVED", result_ok, time.monotonic() - t_r,
                                f"status={result_resp.status_code}"))

    return results
