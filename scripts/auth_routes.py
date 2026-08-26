"""
auth_routes.py
===============
Passwordless email auth + billing status endpoints for SONYA.

  GET  /api/auth/me
  POST /api/auth/request-code
  POST /api/auth/verify-code
  POST /api/auth/logout
  GET  /api/billing/subscription-status

Identity is resolved exclusively from the sonya_session HttpOnly cookie
(see scripts/security.py::get_current_user). The browser never sees a raw
session token's hash, a raw auth code, or the AUTH_SECRET.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from scripts import auth_store
from scripts.auth_security import (
    AUTH_CODE_MAX_ATTEMPTS,
    VALID_PURPOSES,
    clear_session_cookie,
    code_expiry,
    generate_numeric_code,
    generate_session_token,
    hash_code,
    hash_session_token,
    is_valid_email,
    session_expiry,
    set_session_cookie,
)
from scripts.email_sender import (
    EmailNotConfiguredError,
    EmailSendError,
    send_auth_code_email,
    send_welcome_email,
)
from scripts.rate_limiter import check_rate_limit
from scripts.security import (
    get_current_user,
    get_session_token,
    new_trace_id,
    safe_error,
    verify_browser_origin,
)
from scripts.security_audit import audit

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Rate limit config ————————————————————————————————————————————————————————

_REQUEST_CODE_LIMIT_PER_EMAIL_PER_HOUR = 5
_REQUEST_CODE_LIMIT_PER_IP_PER_HOUR = 20
_REQUEST_CODE_MIN_RESEND_SECONDS = 30


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ── Pydantic models ————————————————————————————————————————————————————————————

class RequestCodeBody(BaseModel):
    email: str = Field(..., max_length=254)
    purpose: str = Field(default="login")


class VerifyCodeBody(BaseModel):
    email: str = Field(..., max_length=254)
    code: str = Field(..., min_length=6, max_length=6)
    purpose: str = Field(default="login")
    consents: dict | None = None


# ── Response shaping ————————————————————————————————————————————————————————————

def _user_response(user: dict) -> dict:
    return {
        "user_id": str(user["id"]),
        "email": user["email"],
        "plan_type": user["plan_type"],
        "plan_status": user["plan_status"],
        "plan_active_until": _iso(user.get("plan_active_until")),
        "free_video_limit": user["free_video_limit"],
        "free_video_used": user["free_video_used"],
        "telegram_linked": bool(user.get("telegram_linked", False)),
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


# ── GET /api/auth/me ——————————————————————————————————————————————————————————

@router.get("/api/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    return _user_response(user)


# ── POST /api/auth/request-code ——————————————————————————————————————————————

@router.post("/api/auth/request-code", status_code=status.HTTP_200_OK)
async def request_code(
    body: RequestCodeBody,
    request: Request,
    _origin: None = Depends(verify_browser_origin),
):
    trace_id = new_trace_id()
    ip = _client_ip(request)

    email = body.email.strip().lower()
    purpose = body.purpose.strip().lower() if body.purpose else "login"

    if not is_valid_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_email", "trace_id": trace_id},
        )
    if purpose not in VALID_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_purpose", "allowed": sorted(VALID_PURPOSES), "trace_id": trace_id},
        )

    # ── Rate limiting: sliding window by email and by IP ────────────────────
    if not check_rate_limit(f"auth_code_email:{email}", _REQUEST_CODE_LIMIT_PER_EMAIL_PER_HOUR, 3600):
        logger.warning("[auth] rate_limited_email trace_id=%s", trace_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "trace_id": trace_id},
            headers={"Retry-After": "3600"},
        )
    if not check_rate_limit(f"auth_code_ip:{ip}", _REQUEST_CODE_LIMIT_PER_IP_PER_HOUR, 3600):
        logger.warning("[auth] rate_limited_ip trace_id=%s", trace_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "trace_id": trace_id},
            headers={"Retry-After": "3600"},
        )

    # ── Resend cooldown: block rapid-fire re-requests for the same email ────
    try:
        existing = auth_store.get_latest_pending_code(email, purpose)
        if existing:
            age = (datetime.now(timezone.utc) - existing["created_at"]).total_seconds()
            if age < _REQUEST_CODE_MIN_RESEND_SECONDS:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"error": "rate_limited", "trace_id": trace_id},
                    headers={"Retry-After": str(int(_REQUEST_CODE_MIN_RESEND_SECONDS - age))},
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[auth] resend_cooldown_check_failed trace_id=%s error=%s", trace_id, exc)

    # ── Generate + hash + persist the code ───────────────────────────────────
    code = generate_numeric_code()
    code_hash = hash_code(code, email, purpose)

    try:
        auth_store.create_auth_code(
            email=email,
            code_hash=code_hash,
            purpose=purpose,
            expires_at=code_expiry(),
            ip_address=ip,
        )
    except Exception as exc:
        logger.error("[auth] create_auth_code_failed trace_id=%s error=%s", trace_id, exc)
        raise safe_error("db_error", 500, trace_id)

    # ── Send the email — never accept auth silently without a real send ─────
    try:
        send_auth_code_email(email, code, purpose)
    except EmailNotConfiguredError:
        logger.error("[auth] email_not_configured trace_id=%s", trace_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "email_not_configured", "trace_id": trace_id},
        )
    except EmailSendError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "email_send_failed", "trace_id": trace_id},
        )

    audit("auth_code_requested", trace_id=trace_id,
          details={"purpose": purpose}, ip_address=ip)
    # Raw code is intentionally never logged here.
    logger.info("[auth] code_requested purpose=%s trace_id=%s", purpose, trace_id)

    return {"ok": True}


# ── POST /api/auth/verify-code ——————————————————————————————————————————————

@router.post("/api/auth/verify-code")
async def verify_code(
    body: VerifyCodeBody,
    request: Request,
    response: Response,
    _origin: None = Depends(verify_browser_origin),
):
    trace_id = new_trace_id()
    ip = _client_ip(request)

    email = body.email.strip().lower()
    purpose = body.purpose.strip().lower() if body.purpose else "login"
    submitted_code = body.code.strip()

    if not is_valid_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_email", "trace_id": trace_id},
        )
    if purpose not in VALID_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_purpose", "trace_id": trace_id},
        )
    if not submitted_code.isdigit() or len(submitted_code) != 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_code", "trace_id": trace_id},
        )

    try:
        pending = auth_store.get_latest_pending_code(email, purpose)
    except Exception as exc:
        logger.error("[auth] lookup_code_failed trace_id=%s error=%s", trace_id, exc)
        raise safe_error("db_error", 500, trace_id)

    def _reject(reason: str) -> None:
        audit("auth_code_verify_failed", trace_id=trace_id,
              details={"purpose": purpose, "reason": reason}, ip_address=ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_or_expired_code", "trace_id": trace_id},
        )

    if not pending:
        _reject("no_pending_code")

    now = datetime.now(timezone.utc)
    expires_at = pending["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        _reject("expired")

    if pending["attempts"] >= AUTH_CODE_MAX_ATTEMPTS:
        _reject("max_attempts_exceeded")

    expected_hash = hash_code(submitted_code, email, purpose)
    if expected_hash != pending["code_hash"]:
        attempts = auth_store.increment_code_attempts(pending["id"])
        logger.warning("[auth] code_mismatch attempts=%d trace_id=%s", attempts, trace_id)
        _reject("mismatch")

    # ── Success: consume the code (single use) ───────────────────────────────
    auth_store.consume_code(pending["id"])

    # ── Get or create the user, mark verified ────────────────────────────────
    user = auth_store.get_user_by_email(email)
    is_new_user = user is None
    if is_new_user:
        user = auth_store.get_or_create_user(email)
    auth_store.mark_email_verified(user["id"])
    user = auth_store.get_user_by_id(user["id"])

    # ── Create session, set cookie ────────────────────────————————————————
    token = generate_session_token()
    token_hash = hash_session_token(token)
    auth_store.create_session(
        user_id=user["id"],
        token_hash=token_hash,
        expires_at=session_expiry(),
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=ip,
    )
    set_session_cookie(response, token)

    audit("auth_code_verified", user_id=str(user["id"]), trace_id=trace_id,
          details={"purpose": purpose, "new_user": is_new_user}, ip_address=ip)
    logger.info("[auth] verify_success user_id=%s new_user=%s trace_id=%s",
                user["id"], is_new_user, trace_id)

    # ── Best-effort welcome email for new accounts ───────────────────────────
    # Never blocks or fails the auth response: registration must succeed
    # even if the welcome email can't be sent.
    if is_new_user:
        try:
            send_welcome_email(user["email"])
        except Exception as exc:
            logger.warning("[auth] welcome_email_failed user_id=%s trace_id=%s error=%s",
                            user["id"], trace_id, exc)

    return _user_response(user)


# ── POST /api/auth/logout ————————————————————————————————————————————————————

@router.post("/api/auth/logout")
async def logout(
    request: Request,
    response: Response,
    _origin: None = Depends(verify_browser_origin),
):
    token = get_session_token(request)
    if token:
        token_hash = hash_session_token(token)
        try:
            auth_store.revoke_session_by_token_hash(token_hash)
        except Exception as exc:
            logger.warning("[auth] revoke_session_failed error=%s", exc)

    clear_session_cookie(response)
    return {"ok": True}


# ── GET /api/billing/subscription-status ————————————————————————————————————

@router.get("/api/billing/subscription-status")
async def subscription_status(user: dict = Depends(get_current_user)):
    return {
        "plan_type": user["plan_type"],
        "plan_status": user["plan_status"],
        "plan_active_until": _iso(user.get("plan_active_until")),
        "free_video_limit": user["free_video_limit"],
        "free_video_used": user["free_video_used"],
    }
