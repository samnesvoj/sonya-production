"""
scripts/dev/local_e2e_server.py
================================
NO-GPU local E2E harness for the real streamer batch flow (commit 2e3bddd
and later). Runs the REAL scripts.prod_generation_api:app, unmodified,
against a real local Postgres — the only things this script changes are:

  1. Monkeypatches scripts.streamer_routes' S3 calls (upload_bytes,
     delete_object, generate_presigned_get_url) to a local-disk fake S3
     (scripts/dev/local_fake_s3.py) instead of real S3 -- no external
     storage cost, no production S3 credentials needed. Nothing about
     scripts/prod_s3_storage.py itself is touched; every OTHER caller of
     the real functions (e.g. a real GPU worker, if one were pointed at
     this server) would still hit real S3 exactly as in production.
  2. Adds two harness-only routes that exist ONLY on the `app` object
     inside THIS script's own process -- never in scripts/prod_generation_
     api.py itself, never reachable in a real deployment:
       GET /dev/login   -- creates/reuses one local dev user + a REAL
                            session row (auth_store.create_session, same
                            function a real login uses) and sets the real
                            sonya_session cookie, then redirects to
                            streamer-mode.html. No password/email flow --
                            this is a local test harness, not a security
                            boundary.
       GET /fake-s3/*    -- serves local_fake_s3's root_dir() so a
                            browser can fetch/preview a "completed clip"
                            exactly like it would fetch a real presigned
                            S3 URL.
  3. Mounts the repo root as static files (streamer-mode.html/js/css,
     styles.css, etc.) at "/", so the browser gets the API and the
     frontend from the SAME origin -- no CORS configuration needed.

Usage:
    export DATABASE_URL="postgresql://$(whoami)@localhost/sonya_test"
    python3 scripts/dev/local_e2e_server.py
    # then open http://127.0.0.1:8811/dev/login in a browser

See scripts/dev/fake_streamer_worker.py for the NO-GPU worker that
completes the analyze/compose jobs this server creates.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Must be set before importing scripts.prod_generation_api -- several
# modules read these lazily on first use, but setting them upfront (same
# values every local run) keeps this script deterministic and keeps
# fake_streamer_worker.py (a separate process) in sync via the same
# defaults below.
os.environ.setdefault("APP_ENV", "development")
# Secure=true is the DEFAULT even outside APP_ENV=production (see
# auth_security._cookie_secure()) -- must be explicitly disabled for a
# plain http:// local server, or the browser silently never sends the
# session cookie back and every authenticated request 401s.
os.environ.setdefault("AUTH_COOKIE_INSECURE", "true")
os.environ.setdefault("AUTH_SECRET", "local-e2e-auth-secret-do-not-use-in-production")
os.environ.setdefault("WORKER_SECRET", "local-e2e-worker-secret-do-not-use-in-production")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "local-e2e-telegram-webhook-secret-do-not-use-in-production")
os.environ.setdefault("DATABASE_URL", f"postgresql://{os.environ.get('USER', 'postgres')}@localhost/sonya_test")
LISTEN_PORT = int(os.environ.get("SONYA_LOCAL_E2E_PORT", "8811"))
# CORS_ORIGINS must be this harness's own origin -- prod_generation_api.py
# refuses to start with a '*' wildcard once cookie-based session auth
# (allow_credentials=True) is wired in, even outside APP_ENV=production.
os.environ.setdefault("CORS_ORIGINS", f"http://127.0.0.1:{LISTEN_PORT}")

import uvicorn  # noqa: E402
from fastapi import Response  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from scripts import auth_store, streamer_routes, streamer_notify  # noqa: E402
from scripts.auth_security import generate_session_token, hash_session_token, session_expiry, set_session_cookie  # noqa: E402
from scripts.dev import local_fake_s3  # noqa: E402
from scripts.prod_generation_api import app  # noqa: E402

DEV_USER_EMAIL = "local-e2e-dev@example.com"


# ── 1. Patch streamer_routes' S3 calls onto the local disk ─────────────

def _fake_upload_bytes(content: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    return local_fake_s3.write_bytes(key, content)


streamer_routes.upload_bytes = _fake_upload_bytes
streamer_routes.delete_object = lambda key: local_fake_s3.delete(key)
streamer_routes.generate_presigned_get_url = local_fake_s3.fake_presigned_url


# ── 1b. Patch the Telegram sender -- NEVER make a real call to Telegram's
#        API from this harness (no bot token needed, no real message ever
#        sent). Appends one JSON line per attempted send to a local file
#        under local_fake_s3.root_dir() so a developer can inspect what
#        WOULD have been sent without needing to intercept network
#        traffic. Always "succeeds" (matches Telegram accepting the
#        message) unless SONYA_LOCAL_E2E_FAKE_TELEGRAM_FAIL=true is set,
#        for exercising the failure/retry path locally too. ─────────────

def _fake_telegram_send(chat_id, text, button_text=None, button_url=None) -> bool:
    import json as _json
    import time as _time
    log_path = local_fake_s3.root_dir() / "_fake_telegram_outbox.jsonl"
    with open(log_path, "a") as f:
        f.write(_json.dumps({
            "ts": _time.time(), "chat_id": chat_id, "text": text,
            "button_text": button_text, "button_url": button_url,
        }) + "\n")
    return os.environ.get("SONYA_LOCAL_E2E_FAKE_TELEGRAM_FAIL", "").lower() != "true"


streamer_notify.telegram_bot.send_message = _fake_telegram_send


# ── 2. Harness-only routes (never present in scripts/prod_generation_api.py) ──

@app.get("/dev/login")
async def _dev_login(fresh: bool = False):
    """
    Local test harness only. Creates (or reuses) one fixed local dev
    user, mints a REAL session the same way a real login does
    (auth_store.create_session + set_session_cookie -- not a monkeypatch,
    not a fake cookie value), and redirects into the app. No password —
    this route only exists on THIS script's own `app` object, in this
    process, never in production code.

    ?fresh=1 creates a brand-new random-email user instead of reusing the
    fixed DEV_USER_EMAIL one -- convenience for a manual test session that
    would otherwise trip quota_guard's 24h job-count limit after enough
    repeated local runs against the same dev user.
    """
    email = f"local-e2e-{uuid.uuid4()}@example.com" if fresh else DEV_USER_EMAIL
    user = auth_store.get_or_create_user(email)
    token = generate_session_token()
    auth_store.create_session(
        user_id=user["id"], token_hash=hash_session_token(token),
        expires_at=session_expiry(), user_agent="local-e2e-harness",
    )
    resp = RedirectResponse(url="/streamer-mode.html", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/dev/reset-quota")
async def _dev_reset_quota():
    """Local test harness only -- bump the dev user's free-plan limit so
    repeated manual E2E runs don't need a fresh user every time."""
    user = auth_store.get_user_by_email(DEV_USER_EMAIL)
    if not user:
        return {"ok": False, "reason": "no dev user yet -- visit /dev/login first"}
    conn = auth_store._get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET free_video_used = 0, free_video_limit = 50 WHERE id = %s",
                    (user["id"],),
                )
    finally:
        conn.close()
    return {"ok": True, "user_id": user["id"]}


app.mount("/fake-s3", StaticFiles(directory=str(local_fake_s3.root_dir())), name="fake-s3")

# ── 3. Serve the vanilla-JS frontend from the SAME origin ──────────────
# Mounted LAST: Starlette matches explicit routes (all of scripts/prod_
# generation_api.py's own @app.* routes, plus /dev/* and /fake-s3 above)
# before falling through to this catch-all, so /api/* and /dev/* are
# never shadowed by it.
app.mount("/", StaticFiles(directory=str(REPO_ROOT), html=True), name="static-frontend")


if __name__ == "__main__":
    print(f"[local_e2e_server] fake S3 root:  {local_fake_s3.root_dir()}")
    print(f"[local_e2e_server] DATABASE_URL:  {os.environ['DATABASE_URL']}")
    print(f"[local_e2e_server] WORKER_SECRET: {os.environ['WORKER_SECRET']}")
    print(f"[local_e2e_server] Open:          http://127.0.0.1:{LISTEN_PORT}/dev/login")
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT, log_level="info")
