"""
streamer_routes.py
===================
Real streamer batch flow: SOURCE -> ingest once -> analyze once -> persist
candidate segments -> user selects -> N lightweight compose jobs -> real
clips -> ready / partially_failed. Replaces the mock-backed 3-step flow
streamer-mode.js previously drove entirely client-side.

Browser-authenticated endpoints (sonya_session cookie -- see
scripts.security.get_current_user):
  POST /api/streamer/batches                       create a batch (URL or file source)
  GET  /api/streamer/batches/{batch_id}             batch + segments + clips, owner only
  GET  /api/streamer/batches                        list current user's batches
  POST /api/streamer/batches/{batch_id}/selection   confirm selected segments -> compose jobs

Worker-internal endpoint (Authorization: Bearer WORKER_SECRET, never a
session -- see scripts.security.verify_worker_secret):
  POST /api/worker/streamer/batches/{batch_id}/analysis-result

Source reuse (the whole point of the batch split): the analysis
generation_jobs row's own s3_input_key is copied straight onto every
compose job created by the selection endpoint below -- no second
yt-dlp/direct-URL download, no second S3 upload, no second
enrich_video_for_mode() pass. See scripts/gpu_worker.py's
streamer_phase="analyze"/"compose" branch in process_job() for the worker
side of this split.

Quota: one streamer batch is exactly one billable free-plan quota unit,
charged once via create_job_with_quota() on the analysis job (below, both
the file and URL source paths). The N compose jobs the selection endpoint
creates use the plain, zero-quota create_job() store function directly --
that function is never called from any browser-reachable path except
here, internally, after a batch's one billable action has already gone
through create_job_with_quota().

Deliberately NOT in this pass (see the batch-flow brief this implements):
Telegram completion notifications, scheduled reminders, subscription
messages, TTL/cleanup, a "Мои видео" gallery, real promo/subtitle-rerender
backends, OpenCut integration, or a fix for the dead legacy
analyzer/clipper adapter (modes/streamer/runner.py's analyze() still
falls back to _fallback_segments() -- see that module's own docstring).
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field

from scripts import entitlements, url_ingest
from scripts.prod_job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    STREAMER_BATCH_STATUS_AWAITING_SELECTION,
    STREAMER_BATCH_STATUS_GENERATING,
    StreamerBatchTransitionError,
    StreamerClipJobConflictError,
    add_job_file,
    attach_analysis_job_to_batch,
    attach_streamer_clip_job,
    cancel_orphan_job,
    create_job,
    create_job_with_quota,
    create_streamer_batch,
    get_job,
    get_streamer_batch,
    get_streamer_batch_by_id,
    list_streamer_clip_jobs,
    list_streamer_segments,
    list_user_streamer_batches,
    reconcile_streamer_batch,
    set_streamer_batch_status,
    set_streamer_segment_selection,
    submit_streamer_analysis_result,
)
from scripts.prod_s3_storage import build_input_key, delete_object, generate_presigned_get_url, upload_bytes
from scripts.quota_guard import check_user_quota
from scripts.streamer_notify import notify_streamer_batch_completion
from scripts.rate_limiter import RateLimiter
from scripts.security import get_current_user, new_trace_id, safe_error, verify_browser_origin, verify_worker_secret
from scripts.security_audit import EVT_JOB_CREATED, EVT_UPLOAD_REJECTED, audit
from scripts.upload_security import validate_upload

logger = logging.getLogger(__name__)

router = APIRouter()

# Same priority table as prod_generation_api.py's own _resolve_priority()
# -- duplicated here rather than imported to avoid a circular import
# (prod_generation_api.py imports this module's router). Every module
# in this codebase that needs its own small piece of another module's
# logic re-derives it locally rather than reaching across routers -- same
# convention as each store module's own _get_conn()/_now() helpers.
_PLAN_PRIORITY: dict[str, int] = {
    "admin": 1000, "pro": 500, "paid": 300, "free": 100, "unknown": 100,
}


def _resolve_priority(user: dict) -> tuple[int, str]:
    plan = (user.get("plan_type") or "unknown").lower().strip()
    if plan not in _PLAN_PRIORITY:
        plan = "unknown"
    return _PLAN_PRIORITY[plan], plan


def _is_pro_active(user: dict) -> bool:
    # Legacy (pre-016) Pro only -- new plans are resolved in entitlements.py.
    return entitlements.legacy_pro_active(user)


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


_batch_create_limiter = RateLimiter(key_prefix="streamer_batch_create", limit=20, window_seconds=3600, key_by="session")
_batch_api_limiter    = RateLimiter(key_prefix="streamer_batch_api",    limit=120, window_seconds=60,   key_by="session")


# Same Idempotency-Key format rules as prod_generation_api.py's own
# _validate_idempotency_key() -- duplicated here rather than imported, same
# reasoning as _PLAN_PRIORITY above (avoids a circular import, matches this
# codebase's per-module small-helper convention).
_IDEMPOTENCY_KEY_MAX_LEN = 255
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.:]{1,255}$")


def _validate_idempotency_key(raw: Optional[str], trace_id: str) -> Optional[str]:
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_idempotency_key",
                    "message": "Idempotency-Key must not be empty", "trace_id": trace_id},
        )
    if len(key) > _IDEMPOTENCY_KEY_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_idempotency_key",
                    "message": f"Idempotency-Key must be at most {_IDEMPOTENCY_KEY_MAX_LEN} characters",
                    "trace_id": trace_id},
        )
    if not _IDEMPOTENCY_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_idempotency_key",
                    "message": "Idempotency-Key contains invalid characters "
                                "(allowed: letters, digits, '_', '-', '.', ':')",
                    "trace_id": trace_id},
        )
    return key


def _parse_preset_snapshot(raw: Optional[str]) -> Dict[str, Any]:
    """
    Narrow, allowlisted parse of the client-supplied preset snapshot --
    only telegram_notify_enabled is persisted (streamer_batches.preset_
    snapshot, migration 012), coerced to a real bool. This is the ONE
    place this decision is ever read from the client -- fixed into the
    batch row at creation time and never re-read from the frontend again
    (see scripts/streamer_notify.py for why that matters: the completion
    notification checks THIS stored value, not the user's current preset
    setting, which may have changed since). Malformed/missing input
    defaults to disabled rather than rejecting the whole batch-creation
    request over a client preset quirk -- notification is opt-in, so
    "off" is always the safe default.
    """
    if not raw:
        return {"telegram_notify_enabled": False}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"telegram_notify_enabled": False}
    if not isinstance(parsed, dict):
        return {"telegram_notify_enabled": False}
    return {"telegram_notify_enabled": bool(parsed.get("telegram_notify_enabled"))}


# ── POST /api/streamer/batches ——————————————————————————————————————————————————

@router.post("/api/streamer/batches", status_code=status.HTTP_202_ACCEPTED)
async def create_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    source_type: str = Form(...),
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    preset_snapshot_raw: Optional[str] = Form(None, alias="preset_snapshot"),
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(get_current_user),
    _origin: None = Depends(verify_browser_origin),
    _rl: None = Depends(_batch_create_limiter),
):
    user_id  = str(user["id"])
    trace_id = new_trace_id()
    priority, _plan = _resolve_priority(user)

    if source_type not in ("url", "file"):
        raise HTTPException(status_code=400, detail={"error": "invalid_source_type", "trace_id": trace_id})
    if source_type == "url" and not (url and url.strip()):
        raise HTTPException(status_code=400, detail={"error": "missing_url", "trace_id": trace_id})
    if source_type == "file" and file is None:
        raise HTTPException(status_code=400, detail={"error": "missing_file", "trace_id": trace_id})

    idempotency_key = _validate_idempotency_key(idempotency_key_header, trace_id)
    preset_snapshot = _parse_preset_snapshot(preset_snapshot_raw)

    # Reserve the batch row FIRST, atomically (migration 014), before any
    # quota check or I/O -- a double-click or network retry carrying the
    # SAME Idempotency-Key converges on this single INSERT ... ON CONFLICT
    # DO NOTHING: the losing call gets the winner's own batch back
    # (_created_now=False) and returns immediately, never touching quota,
    # never re-running ingest. idempotency_key=None (no header) always
    # creates a fresh batch -- legacy behavior, unchanged for any client
    # that doesn't send the header yet.
    batch = create_streamer_batch(user_id, preset_snapshot=preset_snapshot, idempotency_key=idempotency_key)
    batch_id = str(batch["id"])
    if not batch["_created_now"]:
        logger.info("[streamer] batch_create_idempotent_replay batch_id=%s user_id=%s trace_id=%s",
                    batch_id, user_id, trace_id)
        return {"batch_id": batch_id, "status": batch["status"]}

    # Quota check before any I/O -- same ordering as create_generation_job
    # / create_generation_job_from_url. Not the enforcement point (that's
    # create_job_with_quota below); this only skips wasted work for the
    # common already-exhausted case. Only ever reached for a genuinely NEW
    # batch (the idempotent-replay branch above already returned), so this
    # can never charge quota twice for the same logical request.
    # Entitlement: streamer_start subscription, legacy Pro, or free quota.
    # Nothing is debited here (create_job_with_quota below does it).
    try:
        check_user_quota(user_id)
        try:
            subscriptions = entitlements.get_user_subscriptions(user_id)
        except Exception as exc:
            logger.error("[streamer] subscriptions_lookup_failed user_id=%s trace_id=%s error_type=%s",
                         user_id, trace_id, type(exc).__name__)
            raise safe_error("db_error", 500, trace_id)
        try:
            ent = entitlements.resolve_entitlement(user, "streamer", subscriptions)
        except entitlements.EntitlementDenied as denied:
            raise HTTPException(status_code=denied.status_code, detail=denied.detail(trace_id))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        set_streamer_batch_status(batch_id, "failed",
                                  error=detail.get("code") or detail.get("error") or "rejected")
        raise
    if ent.kind == "subscription":
        priority = _PLAN_PRIORITY["pro"]

    if source_type == "url":
        try:
            platform = url_ingest.detect_platform(url.strip())
            if platform == "unsupported":
                raise HTTPException(
                    status_code=400,
                    detail={"error": "unsupported_url",
                            "message": url_ingest.UnsupportedUrlError().user_message,
                            "trace_id": trace_id},
                )
            try:
                url_ingest.assert_safe_url(url.strip())
            except url_ingest.UnsafeUrlError:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "unsafe_url", "message": url_ingest.UnsafeUrlError().user_message,
                            "trace_id": trace_id},
                )
        except HTTPException as exc:
            # Batch already exists (reserved above) -- must not be left an
            # orphan "queued" row forever just because the URL itself
            # turned out to be invalid/unsafe.
            set_streamer_batch_status(batch_id, "failed", error=exc.detail.get("error", "invalid_url")
                                       if isinstance(exc.detail, dict) else "invalid_url")
            raise

        background_tasks.add_task(
            _run_streamer_batch_url_ingest,
            batch_id=batch_id, url=url.strip(), platform=platform,
            user_id=user_id, priority=priority, ent=ent,
            client_ip=request.client.host if request.client else None,
        )
        logger.info("[streamer] batch_url_ingest_started batch_id=%s user_id=%s platform=%s trace_id=%s",
                    batch_id, user_id, platform, trace_id)
        return {"batch_id": batch_id, "status": batch["status"]}

    # ── source_type == "file" — synchronous, same shape as
    # create_generation_job()'s own file-upload path (no background task:
    # this is just an S3 upload, not a slow external download). ──────────
    try:
        content, safe_name = await validate_upload(file, max_size_bytes=url_ingest._max_bytes("streamer"))
    except HTTPException as exc:
        set_streamer_batch_status(batch_id, "failed", error="invalid_upload")
        audit(EVT_UPLOAD_REJECTED, user_id=user_id, trace_id=trace_id,
              details={"mode": "streamer", "reason": str(exc.detail)},
              ip_address=request.client.host if request.client else None)
        raise

    # Paid-plan source length on the received bytes -- before S3 / debit.
    if ent.max_source_sec is not None:
        with tempfile.NamedTemporaryFile(suffix=Path(safe_name).suffix or ".mp4") as tmp:
            tmp.write(content)
            tmp.flush()
            try:
                entitlements.measure_and_enforce(ent, tmp.name)
            except entitlements.EntitlementDenied as denied:
                set_streamer_batch_status(batch_id, "failed", error=denied.code)
                raise HTTPException(status_code=denied.status_code, detail=denied.detail(trace_id))

    set_streamer_batch_status(batch_id, "ingesting")

    job_id = str(uuid.uuid4())
    ext    = Path(safe_name).suffix or ".mp4"
    s3_key = build_input_key(user_id=user_id, job_id=job_id, mode="streamer", ext=ext)

    try:
        upload_bytes(content, s3_key, content_type=file.content_type or "video/mp4")
    except Exception as exc:
        logger.error("[streamer] batch_s3_upload_failed batch_id=%s trace_id=%s: %s", batch_id, trace_id, exc)
        set_streamer_batch_status(batch_id, "failed", error="storage_error")
        raise safe_error("storage_error", 500, trace_id)

    try:
        result = create_job_with_quota(
            job_id=job_id, user_id=user_id, mode="streamer",
            params={"streamer_phase": "analyze", "streamer_batch_id": batch_id},
            s3_input_key=s3_key, idempotency_key=None, idempotency_fingerprint=None,
            queue_priority=priority, bypass_quota=ent.kind == "legacy_pro",
            subscription_id=ent.subscription_id,
        )
    except Exception as exc:
        logger.error("[streamer] batch_create_job_failed batch_id=%s trace_id=%s: %s", batch_id, trace_id, exc)
        delete_object(s3_key)
        set_streamer_batch_status(batch_id, "failed", error="db_error")
        raise safe_error("db_error", 500, trace_id)

    if result["outcome"] == "quota_exceeded":
        delete_object(s3_key)
        set_streamer_batch_status(batch_id, "failed", error="FREE_PLAN_USED")
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"error": "payment_required", "code": "FREE_PLAN_USED",
                    "message": "Бесплатная генерация уже использована. Выберите тариф SONYA.",
                    "trace_id": trace_id},
        )
    if result["outcome"] == "plan_limit_reached":
        delete_object(s3_key)
        set_streamer_batch_status(batch_id, "failed", error=entitlements.PLAN_LIMIT_REACHED)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"error": "payment_required", "code": entitlements.PLAN_LIMIT_REACHED,
                    "message": "Лимит тарифа исчерпан или период подписки закончился.",
                    "plan_mode": "streamer", "plan_id": ent.plan_id, "trace_id": trace_id},
        )

    analysis_job_id = str(result["job"]["id"])
    attach_analysis_job_to_batch(batch_id, analysis_job_id)
    try:
        add_job_file(
            job_id=analysis_job_id, user_id=user_id,
            file_type="input", s3_key=s3_key,
            filename=safe_name, content_type=file.content_type or "video/mp4",
            size_bytes=len(content),
        )
    except Exception as exc:
        logger.warning("[streamer] batch_add_input_file_failed job_id=%s: %s", analysis_job_id, exc)

    set_streamer_batch_status(batch_id, "analyzing")
    audit(EVT_JOB_CREATED, user_id=user_id, job_id=analysis_job_id, trace_id=trace_id,
          details={"mode": "streamer", "source": "file", "batch_id": batch_id, "size_bytes": len(content)},
          ip_address=request.client.host if request.client else None)

    return {"batch_id": batch_id, "status": "analyzing"}


def _run_streamer_batch_url_ingest(
    *, batch_id: str, url: str, platform: str, user_id: str,
    priority: int, ent: "entitlements.Entitlement", client_ip: Optional[str],
) -> None:
    """
    Runs in Starlette's threadpool (BackgroundTasks), off the event loop --
    same execution model as prod_generation_api.py's own _run_url_ingest,
    whose probe/download/validate/upload sequence this mirrors exactly.
    The one structural difference: progress and outcome are written to the
    durable streamer_batches.status column (via set_streamer_batch_status)
    instead of the in-memory _url_ingest_store, since a batch must survive
    a page reload / rehydration (see GET /api/streamer/batches/{id}) the
    way a one-shot ingest_id poll never needed to.
    """
    local_path: Optional[str] = None
    try:
        set_streamer_batch_status(batch_id, "ingesting")
        url_ingest.probe(url, platform, mode="streamer", max_duration_sec=ent.max_source_sec)

        local_path, _ext = url_ingest.download_video(url, platform, progress_cb=lambda _pct: None, mode="streamer")

        # Authoritative paid-plan length check on the downloaded file. No debit yet.
        try:
            entitlements.measure_and_enforce(ent, local_path)
        except entitlements.EntitlementDenied as denied:
            set_streamer_batch_status(batch_id, "failed", error=denied.code)
            return

        content, safe_name = url_ingest.validate_downloaded_file(
            local_path, hint_name=f"{platform}_video{Path(local_path).suffix or '.mp4'}", mode="streamer"
        )

        job_id = str(uuid.uuid4())
        s3_key = build_input_key(user_id=user_id, job_id=job_id, mode="streamer",
                                  ext=Path(safe_name).suffix or ".mp4")

        try:
            upload_bytes(content, s3_key, content_type="video/mp4")
        except Exception as exc:
            logger.error("[streamer] batch_url_s3_upload_failed batch_id=%s: %s", batch_id, exc)
            set_streamer_batch_status(batch_id, "failed", error="storage_error")
            return

        try:
            result = create_job_with_quota(
                job_id=job_id, user_id=user_id, mode="streamer",
                params={"streamer_phase": "analyze", "streamer_batch_id": batch_id},
                s3_input_key=s3_key, idempotency_key=None, idempotency_fingerprint=None,
                queue_priority=priority, bypass_quota=ent.kind == "legacy_pro",
                subscription_id=ent.subscription_id,
            )
        except Exception as exc:
            logger.error("[streamer] batch_url_create_job_failed batch_id=%s: %s", batch_id, exc)
            delete_object(s3_key)
            set_streamer_batch_status(batch_id, "failed", error="db_error")
            return

        if result["outcome"] == "quota_exceeded":
            delete_object(s3_key)
            set_streamer_batch_status(batch_id, "failed", error="FREE_PLAN_USED")
            return
        if result["outcome"] == "plan_limit_reached":
            delete_object(s3_key)
            set_streamer_batch_status(batch_id, "failed", error=entitlements.PLAN_LIMIT_REACHED)
            return

        analysis_job_id = str(result["job"]["id"])
        attach_analysis_job_to_batch(batch_id, analysis_job_id)
        try:
            add_job_file(
                job_id=analysis_job_id, user_id=user_id,
                file_type="input", s3_key=s3_key,
                filename=safe_name, content_type="video/mp4",
                size_bytes=len(content),
            )
        except Exception as exc:
            logger.warning("[streamer] batch_url_add_input_file_failed job_id=%s: %s", analysis_job_id, exc)

        audit(EVT_JOB_CREATED, user_id=user_id, job_id=analysis_job_id,
              details={"mode": "streamer", "source": "url", "platform": platform, "batch_id": batch_id},
              ip_address=client_ip)

        set_streamer_batch_status(batch_id, "analyzing")
        logger.info("[streamer] batch_url_ingest_done batch_id=%s job_id=%s", batch_id, analysis_job_id)

    except url_ingest.DownloadLimitExceeded as exc:
        set_streamer_batch_status(batch_id, "failed", error="limit_exceeded")
    except url_ingest.UnsafeUrlError as exc:
        audit(EVT_UPLOAD_REJECTED, user_id=user_id,
              details={"mode": "streamer", "reason": "unsafe_url", "platform": platform, "batch_id": batch_id},
              ip_address=client_ip)
        set_streamer_batch_status(batch_id, "failed", error="unsafe_url")
    except url_ingest.UnsupportedUrlError as exc:
        set_streamer_batch_status(batch_id, "failed", error="unsupported_url")
    except url_ingest.DownloadFailed as exc:
        set_streamer_batch_status(batch_id, "failed", error="download_failed")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        audit(EVT_UPLOAD_REJECTED, user_id=user_id,
              details={"mode": "streamer", "reason": detail, "platform": platform, "source": "url", "batch_id": batch_id},
              ip_address=client_ip)
        set_streamer_batch_status(batch_id, "failed", error=detail.get("error", "invalid_video"))
    except StreamerBatchTransitionError:
        # Batch was already terminal by the time we tried to write to it
        # (e.g. cancelled concurrently) -- nothing left to do.
        logger.info("[streamer] batch_url_ingest_batch_already_terminal batch_id=%s", batch_id)
    except Exception as exc:
        logger.exception("[streamer] batch_url_ingest_unexpected_error batch_id=%s: %s", batch_id, exc)
        try:
            set_streamer_batch_status(batch_id, "failed", error="internal_error")
        except StreamerBatchTransitionError:
            pass
    finally:
        if local_path:
            url_ingest.cleanup(local_path)


# ── GET /api/streamer/batches/{batch_id} ——————————————————————————————————————————

@router.get("/api/streamer/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    _rl: None = Depends(_batch_api_limiter),
):
    user_id = str(user["id"])
    batch = get_streamer_batch(batch_id, user_id)
    if not batch:
        raise HTTPException(status_code=404, detail={"error": "not_found", "trace_id": new_trace_id()})

    # Defensive reconciliation -- see reconcile_streamer_batch()'s own
    # docstring: the primary trigger is the worker/complete and
    # worker/fail hooks in prod_generation_api.py, this is the backstop
    # for the case that was ever missed (worker crash, restart, ...).
    reconciled = reconcile_streamer_batch(batch_id)
    if reconciled:
        batch = reconciled

    # Backgrounded so a browser's GET never waits on Telegram network
    # latency (see scripts/streamer_notify.py's own module docstring) --
    # safe to call unconditionally on every GET, not just when reconcile
    # just changed something: every early-return inside it (wrong status,
    # notify disabled, not linked, already sent, active claim held
    # elsewhere) is a normal, expected no-op.
    background_tasks.add_task(notify_streamer_batch_completion, batch_id)

    segments = list_streamer_segments(batch_id)
    clip_jobs_by_segment = {cj["segment_id"]: cj for cj in list_streamer_clip_jobs(batch_id)}

    clips: List[Dict[str, Any]] = []
    for seg in segments:
        clip_job = clip_jobs_by_segment.get(seg["id"])
        if not clip_job:
            continue
        job = get_job(clip_job["job_id"])
        clip: Dict[str, Any] = {
            "segment_id": seg["id"],
            "job_id": clip_job["job_id"],
            "status": job["status"] if job else "unknown",
            "title": seg["title"],
            "recommended": bool(seg["recommended"]),
            "start_sec": seg["start_sec"],
            "duration_sec": seg["duration_sec"],
        }
        if job and job.get("status") == JOB_STATUS_COMPLETED and job.get("s3_output_key"):
            try:
                preview_url = generate_presigned_get_url(job["s3_output_key"], expires_in=3600)
                clip["previewUrl"]  = preview_url
                clip["downloadUrl"] = preview_url
            except Exception as exc:
                logger.warning("[streamer] batch_presign_failed job_id=%s: %s", clip_job["job_id"], exc)
        if job and job.get("status") == JOB_STATUS_FAILED:
            clip["error"] = job.get("error") or job.get("last_error")
        clips.append(clip)

    return {
        "batch_id": batch["id"],
        "status": batch["status"],
        "error": batch.get("error"),
        "created_at": _iso(batch.get("created_at")),
        "completed_at": _iso(batch.get("completed_at")),
        "segments": [
            {
                "segment_id": s["id"],
                "ordinal": s["ordinal"],
                "start_sec": s["start_sec"],
                "duration_sec": s["duration_sec"],
                "title": s["title"],
                "description": s.get("description"),
                "recommended": bool(s["recommended"]),
                "selected": bool(s["selected"]),
            }
            for s in segments
        ],
        "clips": clips,
        "preset_snapshot": batch.get("preset_snapshot"),
        # Frontend needs this to decide whether it may show the "SONYA
        # will keep working" Telegram-notify hint -- no completion sender
        # exists yet (see module docstring), this is just visibility of
        # the account's current linking state.
        "telegram_linked": bool(user.get("telegram_linked", False)),
    }


# ── GET /api/streamer/batches (list) ——————————————————————————————————————————————

@router.get("/api/streamer/batches")
async def list_batches(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
    _rl: None = Depends(_batch_api_limiter),
):
    user_id = str(user["id"])
    batches = list_user_streamer_batches(user_id, limit=limit, offset=offset)
    return {
        "batches": [
            {"batch_id": b["id"], "status": b["status"], "created_at": _iso(b.get("created_at"))}
            for b in batches
        ]
    }


# ── POST /api/streamer/batches/{batch_id}/selection ————————————————————————————————

class SelectionRequest(BaseModel):
    segment_ids: List[str] = Field(..., min_length=1)


@router.post("/api/streamer/batches/{batch_id}/selection")
async def create_selection(
    batch_id: str,
    body: SelectionRequest,
    user: dict = Depends(get_current_user),
    _origin: None = Depends(verify_browser_origin),
):
    user_id  = str(user["id"])
    trace_id = new_trace_id()

    batch = get_streamer_batch(batch_id, user_id)
    if not batch:
        raise HTTPException(status_code=404, detail={"error": "not_found", "trace_id": trace_id})

    # awaiting_selection: first (normal) call. generating: a retry of an
    # already-processed selection -- allowed through so the loop below can
    # do its idempotent no-op thing instead of erroring on a legitimate
    # double-click / network retry. Anything else (still analyzing, or
    # already ready/failed/cancelled) is a real client-state error.
    if batch["status"] not in (STREAMER_BATCH_STATUS_AWAITING_SELECTION, STREAMER_BATCH_STATUS_GENERATING):
        raise HTTPException(
            status_code=409,
            detail={"error": "invalid_batch_status", "status": batch["status"], "trace_id": trace_id},
        )

    segments = list_streamer_segments(batch_id)
    segment_by_id = {s["id"]: s for s in segments}
    unknown = [sid for sid in body.segment_ids if sid not in segment_by_id]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"error": "foreign_segment", "segment_ids": unknown, "trace_id": trace_id},
        )

    analysis_job = get_job(batch["analysis_job_id"]) if batch.get("analysis_job_id") else None
    if not analysis_job or not analysis_job.get("s3_input_key"):
        raise HTTPException(status_code=409, detail={"error": "analysis_not_ready", "trace_id": trace_id})

    existing_clip_jobs = {cj["segment_id"]: cj for cj in list_streamer_clip_jobs(batch_id)}
    selected_ids = set(body.segment_ids)
    priority, _plan = _resolve_priority(user)

    clip_jobs_out: List[Dict[str, Any]] = []
    for sid in body.segment_ids:
        existing = existing_clip_jobs.get(sid)
        if existing:
            clip_jobs_out.append(existing)
            continue

        seg = segment_by_id[sid]
        job_id = str(uuid.uuid4())
        # Internal, zero-quota job creation -- see module docstring's
        # QUOTA note. Never reachable from the browser on its own.
        create_job(
            job_id=job_id, user_id=user_id, mode="streamer",
            params={
                "streamer_phase": "compose",
                "streamer_batch_id": batch_id,
                "streamer_segment_id": sid,
                "streamer_segment": {"start_sec": seg["start_sec"], "duration_sec": seg["duration_sec"]},
                "streamer_crop_hints": seg.get("crop_hints") or {},
            },
            s3_input_key=analysis_job["s3_input_key"],  # source reuse -- see module docstring
            queue_priority=priority,
        )
        try:
            clip_job = attach_streamer_clip_job(batch_id, sid, job_id)
        except StreamerClipJobConflictError:
            # A concurrent duplicate request already won this segment
            # between our read of existing_clip_jobs above and this
            # attach -- the generation_jobs row just created is an orphan.
            # NOT harmless left alone: get_next_queued_job_for_dispatch()
            # picks up ANY queued row regardless of whether anything is
            # attached to it, so a real GPU worker would eventually claim
            # and process this duplicate. Cancel it (no-op if it somehow
            # got claimed in the tiny race window already -- see
            # cancel_orphan_job's own docstring), then use the winning job
            # instead, same "lost the race" shape as create_job_with_quota's
            # "existing" outcome elsewhere.
            cancel_orphan_job(job_id)
            clip_job = next(
                cj for cj in list_streamer_clip_jobs(batch_id) if cj["segment_id"] == sid
            )
        clip_jobs_out.append(clip_job)

    for seg in segments:
        set_streamer_segment_selection(seg["id"], seg["id"] in selected_ids)

    try:
        batch = set_streamer_batch_status(batch_id, STREAMER_BATCH_STATUS_GENERATING)
    except StreamerBatchTransitionError:
        pass  # already terminal (e.g. cancelled) -- jobs above still exist, harmless

    audit(EVT_JOB_CREATED, user_id=user_id, trace_id=trace_id,
          details={"mode": "streamer", "batch_id": batch_id, "selection_count": len(body.segment_ids)})

    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "clip_jobs": [{"segment_id": cj["segment_id"], "job_id": cj["job_id"]} for cj in clip_jobs_out],
    }


# ── POST /api/worker/streamer/batches/{batch_id}/analysis-result ——————————————————

class WorkerAnalysisResultRequest(BaseModel):
    segments: List[Dict[str, Any]] = []
    crop_hints: Dict[str, Any] = {}
    warnings: List[str] = []
    webcam_boxes_found: Optional[int] = None
    active_speaker_segs: Optional[int] = None


@router.post("/api/worker/streamer/batches/{batch_id}/analysis-result")
async def submit_analysis_result(
    batch_id: str,
    body: WorkerAnalysisResultRequest,
    _auth: None = Depends(verify_worker_secret),
):
    batch = get_streamer_batch_by_id(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    analysis_result = {
        "segments": body.segments,
        "crop_hints": body.crop_hints,
        "warnings": body.warnings,
        "webcam_boxes_found": body.webcam_boxes_found,
        "active_speaker_segs": body.active_speaker_segs,
    }
    try:
        inserted = submit_streamer_analysis_result(batch_id, analysis_result)
    except (KeyError, TypeError) as exc:
        # Malformed segment shape (missing start_sec/duration_sec) -- a
        # worker/analyzer bug, not a transient failure; the worker's own
        # caller treats any non-2xx here as "retry the whole analyze
        # phase", which would just reproduce the same malformed shape, so
        # this is reported clearly rather than retried silently forever.
        raise HTTPException(status_code=400, detail={"error": "invalid_segments", "detail": str(exc)})

    logger.info("[streamer] batch_analysis_result_submitted batch_id=%s segments=%d",
                batch_id, len(inserted))
    return {"ok": True, "batch_id": batch_id, "segments_count": len(inserted)}
