"""
Per-plan entitlements (migration 016, scripts/entitlements.py) -- unit and
route tests without a database. The atomic debit itself is covered against
real Postgres in tests/test_plan_entitlements_postgres.py.

Every refusal test also asserts that nothing was debited: neither the
job-creation store call (where the debit happens) nor the S3 upload ran.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts import auth_store, entitlements, payment_store, url_ingest
from scripts.auth_security import hash_session_token
from tests.conftest import make_session, make_user

NOW = datetime.now(timezone.utc)

PUBLIC_PLANS = {
    # plan_id: (mode, price, ops, max_source_min)
    "cut_start": ("cut", "1090.00", 10, 60),
    "cut_pro": ("cut", "2690.00", 20, 120),
    "cut_studio": ("cut", "4990.00", 30, 180),
    "trailer_start": ("trailer", "1190.00", 8, 90),
    "trailer_pro": ("trailer", "2190.00", 12, 120),
    "trailer_studio": ("trailer", "4990.00", 24, 180),
    "streamer_start": ("streamer", "1990.00", 10, 240),
}


def _sub(plan_id="cut_pro", ops_used=0, start=None, end=None, **overrides):
    mode, _price, ops, max_min = PUBLIC_PLANS[plan_id]
    sub = {
        "id": str(uuid.uuid4()), "plan_id": plan_id, "plan_mode": mode,
        "ops_limit": ops, "ops_used": ops_used, "max_source_sec": max_min * 60,
        "period_start": start or NOW - timedelta(days=1),
        "period_end": end or NOW + timedelta(days=29),
        "payment_id": str(uuid.uuid4()),
    }
    sub.update(overrides)
    return sub


def _free_used():
    return make_user(plan_type="free", free_video_limit=1, free_video_used=1)


# ── resolve_entitlement: mode, limits, expiry ────────────────────────────

@pytest.mark.parametrize("job_mode,plan_id", [
    ("virality", "cut_start"), ("stories", "cut_pro"), ("educational", "cut_studio"),
    ("trailer_film_breaker", "trailer_start"), ("trailer_film_breaker", "trailer_studio"),
    ("streamer", "streamer_start"),
])
def test_subscription_covers_its_own_mode(job_mode, plan_id):
    sub = _sub(plan_id)
    ent = entitlements.resolve_entitlement(_free_used(), job_mode, [sub])
    assert ent.kind == "subscription"
    assert ent.subscription_id == sub["id"] and ent.plan_id == plan_id
    assert ent.max_source_sec == PUBLIC_PLANS[plan_id][3] * 60


@pytest.mark.parametrize("job_mode,plan_id", [
    ("trailer_film_breaker", "cut_pro"), ("streamer", "cut_studio"),
    ("virality", "trailer_pro"), ("streamer", "trailer_studio"),
    ("virality", "streamer_start"), ("trailer_film_breaker", "streamer_start"),
    ("sonya_gen", "cut_studio"),
])
def test_wrong_mode_is_refused(job_mode, plan_id):
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.resolve_entitlement(_free_used(), job_mode, [_sub(plan_id)])
    assert exc.value.code == entitlements.MODE_NOT_IN_PLAN
    assert exc.value.status_code == 402


def test_wrong_mode_falls_back_to_unused_free_quota():
    """A cut subscriber who still has the free video may use it elsewhere."""
    user = make_user(free_video_limit=1, free_video_used=0)
    ent = entitlements.resolve_entitlement(user, "trailer_film_breaker", [_sub("cut_pro")])
    assert ent.kind == "free" and ent.max_source_sec is None


def test_exhausted_subscription_is_refused():
    sub = _sub("cut_start", ops_used=10)
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.resolve_entitlement(make_user(free_video_used=0), "virality", [sub])
    assert exc.value.code == entitlements.PLAN_LIMIT_REACHED
    assert exc.value.extra["ops_limit"] == 10


def test_last_operation_is_still_allowed():
    ent = entitlements.resolve_entitlement(_free_used(), "virality", [_sub("cut_start", ops_used=9)])
    assert ent.kind == "subscription"


def test_expired_subscription_is_refused():
    sub = _sub("trailer_pro", start=NOW - timedelta(days=40), end=NOW - timedelta(days=10))
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.resolve_entitlement(_free_used(), "trailer_film_breaker", [sub])
    assert exc.value.code == entitlements.SUBSCRIPTION_EXPIRED


def test_no_plan_and_no_free_quota_keeps_free_plan_used_code():
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.resolve_entitlement(_free_used(), "virality", [])
    assert exc.value.code == entitlements.FREE_PLAN_USED


def test_free_quota_unchanged_for_user_without_plan():
    ent = entitlements.resolve_entitlement(make_user(free_video_used=0), "virality", [])
    assert ent.kind == "free"


def test_legacy_pro_keeps_what_it_was_sold():
    user = make_user(plan_type="pro", plan_status="active", plan_active_until=NOW + timedelta(days=3),
                     free_video_used=1)
    for mode in ("virality", "trailer_film_breaker", "streamer"):
        ent = entitlements.resolve_entitlement(user, mode, [])
        assert ent.kind == "legacy_pro" and ent.max_source_sec is None


def test_expired_legacy_pro_is_free_user():
    user = make_user(plan_type="pro", plan_status="active", plan_active_until=NOW - timedelta(days=1),
                     free_video_used=1)
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.resolve_entitlement(user, "virality", [])
    assert exc.value.code == entitlements.FREE_PLAN_USED


# ── Source length ────────────────────────────────────────────────────────

@pytest.mark.parametrize("plan_id", sorted(PUBLIC_PLANS))
def test_source_at_limit_passes_and_over_limit_is_refused(plan_id):
    max_sec = PUBLIC_PLANS[plan_id][3] * 60
    ent = entitlements.Entitlement(kind="subscription", plan_mode=PUBLIC_PLANS[plan_id][0],
                                   subscription_id="s", plan_id=plan_id, max_source_sec=max_sec)
    entitlements.enforce_source_duration(ent, max_sec)
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.enforce_source_duration(ent, max_sec + 1)
    assert exc.value.code == entitlements.SOURCE_TOO_LONG
    assert exc.value.status_code == 413
    assert exc.value.extra["max_source_minutes"] == PUBLIC_PLANS[plan_id][3]


def test_unknown_duration_is_refused_for_paid_plan():
    ent = entitlements.Entitlement(kind="subscription", plan_mode="cut", subscription_id="s",
                                   plan_id="cut_pro", max_source_sec=7200)
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.enforce_source_duration(ent, None)
    assert exc.value.code == entitlements.SOURCE_DURATION_UNKNOWN


def test_missing_ffprobe_fails_closed(monkeypatch):
    monkeypatch.setenv("FFPROBE_BIN", "definitely-not-installed-ffprobe")
    ent = entitlements.Entitlement(kind="subscription", plan_mode="cut", subscription_id="s",
                                   plan_id="cut_pro", max_source_sec=7200)
    with pytest.raises(entitlements.EntitlementDenied) as exc:
        entitlements.measure_and_enforce(ent, "/nonexistent.mp4")
    assert exc.value.code == entitlements.DURATION_CHECK_UNAVAILABLE
    assert exc.value.status_code == 503


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg/ffprobe not installed")
def test_ffprobe_measures_real_file_duration(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    duration = entitlements.probe_duration_sec(str(video))
    assert duration == pytest.approx(3.0, abs=0.2)
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 64)
    assert entitlements.probe_duration_sec(str(garbage)) is None


# ── Routes: refusals never debit ─────────────────────────────────────────

def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftyp" + b"\x00" * 64


def _login(client, monkeypatch, user, subs):
    # Unique per test: the upload rate limiter is keyed by session, and a
    # shared token would let this file's uploads exhaust it for the suite.
    token = f"session-{uuid.uuid4()}"
    token_hash = hash_session_token(token)
    session = make_session(user["id"], token_hash)
    monkeypatch.setattr(auth_store, "get_active_session_by_token_hash", lambda th: session if th == token_hash else None)
    monkeypatch.setattr(auth_store, "get_user_by_id", lambda uid: user if uid == user["id"] else None)
    monkeypatch.setattr(entitlements, "get_user_subscriptions", lambda user_id: subs)
    client.cookies.set("sonya_session", token)


@pytest.fixture()
def job_spy(monkeypatch):
    """Records every create_job_with_quota (= debit) and S3 upload call."""
    calls = {"create": [], "upload": [], "deleted": []}
    outcome = {"value": "created"}

    def fake_create_job_with_quota(**kw):
        calls["create"].append(kw)
        if outcome["value"] != "created":
            return {"outcome": outcome["value"], "job": None}
        return {"outcome": "created", "job": {"id": kw["job_id"], "user_id": kw["user_id"],
                                               "mode": kw["mode"], "status": "queued", "created_at": None}}

    monkeypatch.setattr("scripts.prod_generation_api.create_job_with_quota", fake_create_job_with_quota)
    monkeypatch.setattr("scripts.prod_generation_api.upload_bytes",
                        lambda content, key, content_type=None: calls["upload"].append(key))
    monkeypatch.setattr("scripts.prod_generation_api.delete_object", lambda key: calls["deleted"].append(key))
    monkeypatch.setattr("scripts.prod_generation_api.get_job", lambda job_id: {"created_at": None})
    monkeypatch.setattr("scripts.prod_generation_api.add_job_file", lambda **kw: "file-id")
    monkeypatch.setattr("scripts.prod_generation_api.build_input_key",
                        lambda user_id, job_id, mode, ext: f"users/{user_id}/jobs/{job_id}/{mode}/input/file{ext}")

    def _no_db_conn():
        raise RuntimeError("no DB available in tests")
    monkeypatch.setattr("scripts.prod_job_store._get_conn", _no_db_conn)
    calls["outcome"] = outcome
    return calls


def _post_job(client, mode="virality"):
    return client.post("/api/generation/jobs", data={"mode": mode},
                       files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")})


def test_subscriber_job_is_billed_to_the_subscription(client, monkeypatch, job_spy):
    sub = _sub("cut_pro")
    _login(client, monkeypatch, _free_used(), [sub])
    monkeypatch.setattr(entitlements, "probe_duration_sec", lambda path: 30 * 60)

    resp = _post_job(client)
    assert resp.status_code == 202, resp.text
    [call] = job_spy["create"]
    assert call["subscription_id"] == sub["id"]
    assert call["bypass_quota"] is False


def test_too_long_source_is_refused_before_upload_and_debit(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [_sub("cut_pro")])
    monkeypatch.setattr(entitlements, "probe_duration_sec", lambda path: 121 * 60)

    resp = _post_job(client)
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert detail["code"] == "SOURCE_TOO_LONG" and detail["max_source_minutes"] == 120
    assert job_spy["create"] == [] and job_spy["upload"] == []


def test_client_cannot_claim_a_shorter_duration(client, monkeypatch, job_spy):
    """Only the server's own measurement counts -- a duration in params is ignored."""
    _login(client, monkeypatch, _free_used(), [_sub("trailer_start")])
    monkeypatch.setattr(entitlements, "probe_duration_sec", lambda path: 100 * 60)
    resp = client.post("/api/generation/jobs",
                       data={"mode": "trailer_film_breaker", "params": '{"duration_sec": 60}'},
                       files={"file": ("clip.mp4", io.BytesIO(_mp4_bytes()), "video/mp4")})
    assert resp.status_code == 413
    assert job_spy["create"] == []


def test_wrong_mode_is_refused_before_upload_and_debit(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [_sub("cut_studio")])
    resp = _post_job(client, mode="trailer_film_breaker")
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "MODE_NOT_IN_PLAN"
    assert resp.json()["detail"]["plan_mode"] == "trailer"
    assert job_spy["create"] == [] and job_spy["upload"] == []


def test_exhausted_plan_is_refused_before_upload_and_debit(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [_sub("cut_start", ops_used=10)])
    resp = _post_job(client)
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "PLAN_LIMIT_REACHED"
    assert job_spy["create"] == [] and job_spy["upload"] == []


def test_expired_plan_is_refused(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(),
           [_sub("cut_pro", start=NOW - timedelta(days=31), end=NOW - timedelta(days=1))])
    resp = _post_job(client)
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "SUBSCRIPTION_EXPIRED"
    assert job_spy["create"] == []


def test_invalid_mode_validation_never_debits(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [_sub("cut_pro")])
    resp = _post_job(client, mode="not_a_mode")
    assert resp.status_code == 400
    assert job_spy["create"] == []


def test_lost_race_on_last_operation_returns_402_and_cleans_up(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [_sub("cut_start", ops_used=9)])
    monkeypatch.setattr(entitlements, "probe_duration_sec", lambda path: 60)
    job_spy["outcome"]["value"] = "plan_limit_reached"
    resp = _post_job(client)
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "PLAN_LIMIT_REACHED"
    assert job_spy["deleted"] == job_spy["upload"]  # orphaned upload removed


def test_free_plan_flow_unchanged_and_never_probes_duration(client, monkeypatch, job_spy):
    _login(client, monkeypatch, make_user(free_video_used=0), [])

    def _must_not_probe(path):
        raise AssertionError("free plan must not be gated on ffprobe")
    monkeypatch.setattr(entitlements, "probe_duration_sec", _must_not_probe)
    resp = _post_job(client)
    assert resp.status_code == 202, resp.text
    [call] = job_spy["create"]
    assert call["subscription_id"] is None and call["bypass_quota"] is False


def test_free_quota_used_still_opens_paywall_code(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [])
    resp = _post_job(client)
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "FREE_PLAN_USED"
    assert job_spy["create"] == []


# ── URL path: plan cap replaces the 60-min default, checked after download ─

def test_url_ingest_uses_plan_cap_and_refuses_long_download(client, monkeypatch, job_spy, tmp_path):
    _login(client, monkeypatch, _free_used(), [_sub("cut_studio")])
    seen = {}

    def _probe(url, platform, mode=None, max_duration_sec=None):
        seen["cap"] = max_duration_sec
        return {"duration": None}
    video = tmp_path / "v.mp4"
    video.write_bytes(_mp4_bytes())
    monkeypatch.setattr(url_ingest, "probe", _probe)
    monkeypatch.setattr(url_ingest, "download_video",
                        lambda url, platform, progress_cb=None, mode=None: (str(video), ".mp4"))
    monkeypatch.setattr(entitlements, "probe_duration_sec", lambda path: 181 * 60)

    start = client.post("/api/generation/jobs/from-url",
                        json={"url": "https://www.youtube.com/watch?v=abc", "mode": "virality"})
    assert start.status_code == 202, start.text
    status_resp = client.get(f"/api/generation/jobs/from-url/{start.json()['ingest_id']}").json()
    assert seen["cap"] == 180 * 60          # not the generic 60-minute default
    assert status_resp["status"] == "failed" and status_resp["error"] == "SOURCE_TOO_LONG"
    assert job_spy["create"] == [] and job_spy["upload"] == []


def test_url_ingest_wrong_mode_refused_before_download(client, monkeypatch, job_spy):
    _login(client, monkeypatch, _free_used(), [_sub("trailer_pro")])
    monkeypatch.setattr(url_ingest, "download_video",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    resp = client.post("/api/generation/jobs/from-url",
                       json={"url": "https://www.youtube.com/watch?v=abc", "mode": "stories"})
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "MODE_NOT_IN_PLAN"


# ── Streamer batch ───────────────────────────────────────────────────────

def test_streamer_batch_refused_for_cut_plan(client, monkeypatch):
    _login(client, monkeypatch, _free_used(), [_sub("cut_studio")])
    statuses = []
    monkeypatch.setattr("scripts.streamer_routes.create_streamer_batch",
                        lambda user_id, preset_snapshot=None, idempotency_key=None:
                        {"id": "b1", "status": "queued", "_created_now": True})
    monkeypatch.setattr("scripts.streamer_routes.set_streamer_batch_status",
                        lambda batch_id, status, error=None: statuses.append((status, error)))
    monkeypatch.setattr("scripts.streamer_routes.create_job_with_quota",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not debit")))
    monkeypatch.setattr("scripts.streamer_routes.check_user_quota", lambda user_id: None)

    resp = client.post("/api/streamer/batches", data={"source_type": "url", "url": "https://twitch.tv/videos/1"})
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "MODE_NOT_IN_PLAN"
    assert statuses == [("failed", "MODE_NOT_IN_PLAN")]


# ── Checkout ─────────────────────────────────────────────────────────────

class _FakeProvider:
    """Stands in for whichever provider is active -- entitlement and
    pricing behavior must not depend on a specific one."""
    name = "fake"

    def is_test_mode(self):
        return True

    def create_checkout(self, payment, plan):
        return f"https://pay.example/checkout?order={payment['invoice_id']}&sum={plan.amount}"


def _checkout_env(monkeypatch):
    monkeypatch.setattr("scripts.payment_routes.get_checkout_provider", lambda: _FakeProvider())


@pytest.mark.parametrize("plan_id", sorted(PUBLIC_PLANS))
def test_checkout_snapshots_server_terms_and_ignores_client_price(client, monkeypatch, plan_id):
    _checkout_env(monkeypatch)
    _login(client, monkeypatch, _free_used(), [])
    captured = {}

    def fake_create_pending_payment(**kw):
        captured.update(kw)
        return {"id": "p1", "invoice_id": 777, **kw, "status": "pending"}
    monkeypatch.setattr(payment_store, "create_pending_payment", fake_create_pending_payment)

    resp = client.post("/api/billing/checkout",
                       json={"plan_id": plan_id, "amount": "1.00", "price": 1, "ops_limit": 9999})
    assert resp.status_code == 200, resp.text
    mode, price, ops, max_min = PUBLIC_PLANS[plan_id]
    assert captured["amount"] == Decimal(price)
    assert (captured["plan_mode"], captured["ops_limit"], captured["max_source_sec"]) == (mode, ops, max_min * 60)
    assert captured["provider"] == "fake"
    assert resp.json()["redirect_url"].endswith(f"&sum={price}")


def test_checkout_rejects_unknown_plan(client, monkeypatch):
    _checkout_env(monkeypatch)
    _login(client, monkeypatch, _free_used(), [])
    for bad in ("pro_30d", "streamer_pro", "streamer_max", "cut_ultra", ""):
        resp = client.post("/api/billing/checkout", json={"plan_id": bad})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "unknown_plan"


def test_checkout_refuses_second_purchase_while_same_mode_has_ops_left(client, monkeypatch):
    _checkout_env(monkeypatch)
    _login(client, monkeypatch, _free_used(), [_sub("cut_start", ops_used=3)])
    monkeypatch.setattr(payment_store, "create_pending_payment",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not create payment")))
    resp = client.post("/api/billing/checkout", json={"plan_id": "cut_studio"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "subscription_active"
    assert resp.json()["detail"]["subscription"]["ops_remaining"] == 7


@pytest.mark.parametrize("subs", [
    [_sub("cut_start", ops_used=10)],                                              # exhausted
    [_sub("cut_start", start=NOW - timedelta(days=40), end=NOW - timedelta(days=10))],  # expired
    [_sub("trailer_pro")],                                                         # other mode
])
def test_checkout_allowed_when_same_mode_is_exhausted_expired_or_other_mode(client, monkeypatch, subs):
    _checkout_env(monkeypatch)
    _login(client, monkeypatch, _free_used(), subs)
    monkeypatch.setattr(payment_store, "create_pending_payment",
                        lambda **kw: {"id": "p1", "invoice_id": 778, **kw, "status": "pending"})
    resp = client.post("/api/billing/checkout", json={"plan_id": "cut_pro"})
    assert resp.status_code == 200, resp.text


# ── /api/auth/me exposes the entitlement, nothing financial ─────────────

def test_me_exposes_active_plan_usage_and_expiry(client, monkeypatch):
    sub = _sub("trailer_pro", ops_used=5)
    expired = _sub("cut_pro", start=NOW - timedelta(days=40), end=NOW - timedelta(days=10))
    _login(client, monkeypatch, _free_used(), [sub, expired])
    body = client.get("/api/auth/me").json()
    assert body["subscriptions"] == [{
        "plan_id": "trailer_pro", "mode": "trailer", "ops_limit": 12, "ops_used": 5,
        "ops_remaining": 7, "max_source_minutes": 120,
        "period_start": sub["period_start"].isoformat(), "period_end": sub["period_end"].isoformat(),
    }]
    flat = str(body)
    for internal in ("amount", "payment_id", "price", "gpu", "margin"):
        assert internal not in flat
