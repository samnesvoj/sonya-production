"""
Real PostgreSQL integration tests for scripts.prod_job_store.create_job_with_quota.

Covers the two properties a mocked test cannot prove (both depend on
Postgres's own row-level locking, see the function's docstring):

  1. N genuinely concurrent free-plan job-creation attempts for the SAME
     user with free_video_limit=1 produce exactly one "created" job --
     the rest get "quota_exceeded", never more than one unit of quota
     spent no matter how many requests race.
  2. A replay of an Idempotency-Key that already exhausted the user's
     quota returns the original job ("existing"), not "quota_exceeded" --
     the idempotency-key check happens before quota is even considered.

Skipped automatically unless DATABASE_URL is set (same convention as
tests/test_job_idempotency_postgres.py and tests/test_payment_atomicity_postgres.py).
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres job-quota atomicity test skipped locally",
)


@pytest.fixture()
def stores():
    from scripts import auth_store, prod_job_store
    return auth_store, prod_job_store


def _mk_user(auth_store, free_video_limit=1):
    user = auth_store.create_user(f"quota-test-{uuid.uuid4()}@example.com")
    # create_user() only sets email; give it a known, tight limit for
    # these tests directly via a raw update (no auth_store setter exists
    # for this column, and adding one would be scope creep for this fix).
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET free_video_limit = %s, free_video_used = 0 WHERE id = %s",
                    (free_video_limit, user["id"]),
                )
    finally:
        conn.close()
    return auth_store.get_user_by_id(user["id"])


def _job_kwargs(user_id, **overrides):
    kwargs = dict(
        job_id=str(uuid.uuid4()),
        user_id=user_id,
        mode="virality",
        params={"a": 1},
        s3_input_key=f"users/{user_id}/jobs/{uuid.uuid4()}/virality/input/file.mp4",
        idempotency_key=None,
        idempotency_fingerprint=None,
        queue_priority=0,
        bypass_quota=False,
    )
    kwargs.update(overrides)
    return kwargs


def test_concurrent_requests_never_exceed_free_video_limit(stores):
    auth_store, prod_job_store = stores
    user = _mk_user(auth_store, free_video_limit=1)
    n = 8

    def attempt(_i):
        return prod_job_store.create_job_with_quota(**_job_kwargs(user["id"]))

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    created = [r for r in results if r["outcome"] == "created"]
    exceeded = [r for r in results if r["outcome"] == "quota_exceeded"]

    assert len(created) == 1, f"expected exactly one created job, got {len(created)}: {results}"
    assert len(exceeded) == n - 1

    updated_user = auth_store.get_user_by_id(user["id"])
    assert updated_user["free_video_used"] == 1, "quota must be spent exactly once, not N times"


def test_replay_after_limit_exhausted_returns_existing_job_not_402(stores):
    auth_store, prod_job_store = stores
    user = _mk_user(auth_store, free_video_limit=1)
    key = f"replay-{uuid.uuid4()}"

    first = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], idempotency_key=key, idempotency_fingerprint="fp-a"))
    assert first["outcome"] == "created"

    updated_user = auth_store.get_user_by_id(user["id"])
    assert updated_user["free_video_used"] == 1  # limit now exhausted

    # A retry with the SAME Idempotency-Key must return the original job,
    # not "quota_exceeded" -- even though quota is fully spent.
    replay = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], idempotency_key=key, idempotency_fingerprint="fp-a"))
    assert replay["outcome"] == "existing"
    assert replay["job"]["id"] == first["job"]["id"]

    # Replay must not spend quota a second time.
    final_user = auth_store.get_user_by_id(user["id"])
    assert final_user["free_video_used"] == 1

    # A genuinely NEW request (different key) for the same exhausted user
    # correctly gets quota_exceeded.
    other = prod_job_store.create_job_with_quota(**_job_kwargs(user["id"], idempotency_key=f"other-{uuid.uuid4()}", idempotency_fingerprint="fp-b"))
    assert other["outcome"] == "quota_exceeded"


def test_concurrent_replays_of_same_key_all_return_existing_job(stores):
    """Two concurrent requests with the SAME never-before-seen key, for a
    user with exactly one quota unit -- exactly one must "create", and the
    other must see it as "existing" (a replay), never "quota_exceeded".
    Without the per-user row lock in create_job_with_quota, both could
    observe "no existing row" before either commits and both attempt to
    spend quota."""
    auth_store, prod_job_store = stores
    user = _mk_user(auth_store, free_video_limit=1)
    key = f"concurrent-dup-{uuid.uuid4()}"
    n = 5

    def attempt(_i):
        return prod_job_store.create_job_with_quota(
            **_job_kwargs(user["id"], idempotency_key=key, idempotency_fingerprint="fp-shared")
        )

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    created = [r for r in results if r["outcome"] == "created"]
    existing = [r for r in results if r["outcome"] == "existing"]
    exceeded = [r for r in results if r["outcome"] == "quota_exceeded"]

    assert len(created) == 1, f"expected exactly one creator, got {results}"
    assert len(existing) == n - 1, f"expected the rest to see it as a replay, got {results}"
    assert len(exceeded) == 0, "a duplicate-key race must never be misreported as quota_exceeded"

    winner_job_id = created[0]["job"]["id"]
    assert all(r["job"]["id"] == winner_job_id for r in existing)

    updated_user = auth_store.get_user_by_id(user["id"])
    assert updated_user["free_video_used"] == 1
