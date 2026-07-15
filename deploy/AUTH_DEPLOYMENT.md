# Auth/Users Layer — Deployment Guide

Passwordless email auth (`/api/auth/*`, `/api/billing/subscription-status`)
for `https://sonya-e.com`. This adds three tables (`users`, `auth_codes`,
`sessions`), two new env vars (`AUTH_SECRET`, `SMTP_*`), and changes how
`user_id` is resolved on the browser-facing generation endpoints (from the
`sonya_session` cookie instead of the `X-User-Id` header).

No existing tables, worker endpoints, or the `generation_jobs` schema are
changed. `WORKER_SECRET`-based worker auth is completely untouched.

---

## 1. New/changed env vars

Add to `/srv/sonya/.env` (see `.env.example` for the full reference):

```bash
# Required — auth code / session HMAC secret. MUST differ from WORKER_SECRET.
AUTH_SECRET=$(openssl rand -hex 32)

# Required for /api/auth/request-code to actually send codes.
# Until these are set, request-code returns 503 {"error":"email_not_configured"}.
SMTP_HOST=smtp.yourprovider.com
SMTP_PORT=587
SMTP_USERNAME=apikey-or-username
SMTP_PASSWORD=CHANGE_ME
SMTP_FROM_EMAIL=noreply@sonya-e.com
SMTP_FROM_NAME=SONYA

# Already required, now also validated at import time for the CORS+cookie
# combination — must be the exact production origins, no wildcard:
CORS_ORIGINS=https://sonya-e.com,https://www.sonya-e.com
APP_ENV=production
```

`AUTH_SECRET` and `CORS_ORIGINS` are now enforced by
`scripts/prod_preflight_check.py --role backend` — the API will refuse to
start without them (`AUTH_SECRET` is required at request time by
`scripts/auth_security.py`; `CORS_ORIGINS='*'` is rejected at import time
in `scripts/prod_generation_api.py` because it's incompatible with
cookie-based (`allow_credentials=True`) CORS).

## 2. Run the migration

```bash
cd /srv/sonya
source .venv/bin/activate
git pull
pip install -r requirements-backend.txt   # no new deps, but keep in sync
python scripts/run_migrations.py
```

This applies `scripts/migrations/008_auth_users_sessions.sql`, which is
idempotent (`IF NOT EXISTS` throughout) — safe to re-run. It creates:

- `users` — one row per email, plan/quota fields
- `auth_codes` — hashed 6-digit codes (never the raw code)
- `sessions` — hashed opaque session tokens (never the raw cookie value)

Verify:

```bash
psql "$DATABASE_URL" -c "\d users"
psql "$DATABASE_URL" -c "\d auth_codes"
psql "$DATABASE_URL" -c "\d sessions"
```

## 3. Pre-flight check + restart

```bash
python scripts/prod_preflight_check.py --role backend
systemctl restart sonya-api
systemctl status sonya-api --no-pager
journalctl -u sonya-api -n 50 --no-pager
```

Nothing changes for the dispatcher/worker services
(`sonya-dispatcher`, GPU workers) — they don't need a restart for this
change, but restarting them is harmless if convenient.

## 4. curl tests

Replace `$API` with `https://sonya-e.com/api` (or `http://127.0.0.1:8000/api`
if testing on the VPS directly before the reverse proxy is in front of it).
`-c` / `-b` persist the `sonya_session` cookie across calls, same as a
browser would.

```bash
API=https://sonya-e.com/api
JAR=/tmp/sonya_cookies.txt
rm -f "$JAR"

# 1) Health check
curl -sS "$API/health"

# 2) Request a code (check your inbox / SMTP provider logs for the 6-digit code)
curl -sS -X POST "$API/auth/request-code" \
  -H "Content-Type: application/json" \
  -H "Origin: https://sonya-e.com" \
  -d '{"email":"you@example.com","purpose":"register"}'
# -> {"ok": true}

# 3) Verify the code (replace 123456 with the real code from the email)
curl -sS -c "$JAR" -X POST "$API/auth/verify-code" \
  -H "Content-Type: application/json" \
  -H "Origin: https://sonya-e.com" \
  -d '{"email":"you@example.com","code":"123456","purpose":"register"}'
# -> user object: {"user_id": "...", "email": "you@example.com", "plan_type": "free", ...}
# The Set-Cookie: sonya_session=...; HttpOnly; Secure; SameSite=lax; Path=/ header is saved to $JAR.

# 4) Call /me using the saved cookie
curl -sS -b "$JAR" "$API/auth/me"
# -> same user object as step 3

# 5) Billing/subscription status
curl -sS -b "$JAR" "$API/billing/subscription-status"
# -> {"plan_type": "free", "plan_status": "active", ..., "free_video_limit": 1, "free_video_used": 0}

# 6) Confirm the old X-User-Id header is now ignored on browser endpoints
curl -sS -o /dev/null -w "%{http_code}\n" "$API/generation/jobs" -H "X-User-Id: attacker-id"
# -> 401 (no cookie -> unauthorized, header is not trusted)

# 7) List jobs using the real session (should be empty for a fresh account)
curl -sS -b "$JAR" "$API/generation/jobs"
# -> {"jobs": [], "count": 0}

# 8) Logout
curl -sS -b "$JAR" -c "$JAR" -X POST "$API/auth/logout" \
  -H "Origin: https://sonya-e.com"
# -> {"ok": true}, and sonya_session is cleared in $JAR

# 9) Confirm the session no longer works
curl -sS -o /dev/null -w "%{http_code}\n" -b "$JAR" "$API/auth/me"
# -> 401

# 10) SMTP-not-configured error contract (only meaningful if SMTP_* vars are
#     unset — should NOT happen in production, use this to test staging):
#     -> 503 {"error": "email_not_configured"}

# 11) Worker endpoints are completely unaffected — still WORKER_SECRET only:
curl -sS -X POST "$API/worker/claim" \
  -H "Authorization: Bearer $WORKER_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"worker_id":"smoke-test"}'
# -> {"job": null} or a claimed job, exactly as before this change.
```

## 5. Frontend integration notes (no localStorage required)

- The frontend must call every `/api/auth/*` and `/api/billing/*` (and now
  `/api/generation/*`) request with `credentials: "include"` (fetch) or
  `withCredentials: true` (axios) so the browser sends/receives the
  `sonya_session` cookie.
- The frontend must NOT set `X-User-Id` — it is ignored on all
  browser-facing endpoints as of this change.
- `sonya_session` is `HttpOnly` — JavaScript cannot read it, which is
  intentional. Auth state should be derived from calling `GET /api/auth/me`
  (401 = logged out, 200 = logged in) rather than checking for a cookie.

## 6. Rollback

The migration only adds new tables/columns — no destructive changes to
`generation_jobs`/`generation_files`. To roll back the *code* (not the
schema, which is safe to leave in place):

```bash
git revert <this-change-sha>
systemctl restart sonya-api
```

If you must also drop the new tables:

```sql
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS auth_codes;
DROP TABLE IF EXISTS users;
```
