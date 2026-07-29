"""
Real PostgreSQL 15 integration test for the idempotency unique index
(migration 009). A pure-mocked test cannot verify that
ON CONFLICT (user_id, idempotency_key) DO NOTHING actually serializes
concurrent inserts at the database level -- that guarantee comes entirely
from Postgres's own unique-index locking, which no amount of Python-level
monkeypatching exercises. This test hits a real, migrated database.

Skipped automatically unless DATABASE_URL is set (e.g. the dedicated CI
step with a postgres service container -- see .github/workflows/test.yml).
Local run:
    createdb sonya_idempotency_test
    DATABASE_URL=postgresql://localhost/sonya_idempotency_test \
        python scripts/run_migrations.py
    DATABASE_URL=postgresql://localhost/sonya_idempotency_test \
        pytest tests/test_job_idempotency_postgres.py -v
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres idempotency test skipped locally",
)


@pytest.fixture()
def job_store():
    # Imported lazily, after the skipif above has already decided whether
    # this module's tests run at all, and after DATABASE_URL is known to
    # be set (see root conftest.py for the non-DB env vars pytest needs).
    from scripts import prod_job_store
    return prod_job_store


def _mk_row(job_id, user_id, mode="virality", s3_input_key="k", **overrides):
    row = dict(
        job_id=job_id, user_id=user_id, mode=mode, params={"a": 1},
        s3_input_key=s3_input_key, queue_priority=0,
    )
    row.update(overrides)
    return row


def test_same_key_sequential_creates_one_job(job_store):
    user_id = str(uuid.uuid4())
    key = f"seq-{uuid.uuid4()}"

    first = job_store.create_job_idempotent(
        **_mk_row(str(uuid.uuid4()), user_id, s3_input_key="k1"),
        idempotency_key=key, idempotency_fingerprint="fp-a",
    )
    second = job_store.create_job_idempotent(
        **_mk_row(str(uuid.uuid4()), user_id, s3_input_key="k2"),
        idempotency_key=key, idempotency_fingerprint="fp-a",
    )

    assert first is not None
    assert second is None  # conflict -- caller must fetch the existing row

    existing = job_store.get_job_by_idempotency_key(user_id, key)
    assert existing["id"] == first["id"]


def test_different_users_same_key_get_different_jobs(job_store):
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    key = f"shared-{uuid.uuid4()}"

    row_a = job_store.create_job_idempotent(
        **_mk_row(str(uuid.uuid4()), user_a, s3_input_key="ka"),
        idempotency_key=key, idempotency_fingerprint="fp-a",
    )
    row_b = job_store.create_job_idempotent(
        **_mk_row(str(uuid.uuid4()), user_b, s3_input_key="kb"),
        idempotency_key=key, idempotency_fingerprint="fp-b",
    )

    assert row_a is not None
    assert row_b is not None
    assert row_a["id"] != row_b["id"]


def test_missing_key_always_creates_a_new_job(job_store):
    """NULL idempotency_key never conflicts with anything (or itself)."""
    user_id = str(uuid.uuid4())

    row1 = job_store.create_job_idempotent(
        **_mk_row(str(uuid.uuid4()), user_id, s3_input_key="k1"),
        idempotency_key=None, idempotency_fingerprint=None,
    )
    row2 = job_store.create_job_idempotent(
        **_mk_row(str(uuid.uuid4()), user_id, s3_input_key="k2"),
        idempotency_key=None, idempotency_fingerprint=None,
    )

    assert row1 is not None
    assert row2 is not None
    assert row1["id"] != row2["id"]


def test_concurrent_inserts_with_same_key_produce_exactly_one_job(job_store):
    """
    The real regression target: N genuinely concurrent connections racing
    to INSERT ... ON CONFLICT (user_id, idempotency_key) DO NOTHING for the
    SAME key. Exactly one must win; every other call must observe the
    conflict (return None) rather than each creating its own row. This is
    the property no mocked test can prove -- it depends entirely on
    PostgreSQL's own unique-index insert locking.
    """
    user_id = str(uuid.uuid4())
    key = f"race-{uuid.uuid4()}"
    n = 8

    def attempt(i):
        return job_store.create_job_idempotent(
            **_mk_row(str(uuid.uuid4()), user_id, s3_input_key=f"k{i}"),
            idempotency_key=key, idempotency_fingerprint="fp-race",
        )

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(attempt, range(n)))

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]

    assert len(winners) == 1, f"expected exactly one winning insert, got {len(winners)}"
    assert len(losers) == n - 1

    existing = job_store.get_job_by_idempotency_key(user_id, key)
    assert existing["id"] == winners[0]["id"]
