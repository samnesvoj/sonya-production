"""
Real PostgreSQL integration tests for streamer_batches / streamer_segments
/ streamer_clip_jobs (migration 012). Mirrors tests/test_job_idempotency_
postgres.py's approach: FK constraints, ON DELETE CASCADE, ON CONFLICT
uniqueness, and the terminal-status guard are all things a hand-rolled
fake cursor can get subtly wrong — this hits a real, migrated database
instead, same as the existing _postgres-suffixed tests in this directory.

Skipped automatically unless DATABASE_URL is set.
Local run (same DB the rest of this project's _postgres tests already use):
    export DATABASE_URL="postgresql://localhost/sonya_test"
    python scripts/run_migrations.py
    pytest tests/test_streamer_batches_postgres.py -v

streamer_batches.user_id and streamer_clip_jobs.job_id both carry REAL
foreign keys (unlike generation_jobs.user_id, which is plain TEXT with no
FK) -- every test that needs a user_id/job_id creates a real parent row
first via auth_store.create_user() / prod_job_store.create_job(), not a
bare random uuid.
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- real-Postgres streamer_batches test skipped locally",
)


@pytest.fixture()
def store():
    from scripts import prod_job_store
    return prod_job_store


@pytest.fixture()
def user_id():
    from scripts import auth_store
    user = auth_store.create_user(f"batch-test-{uuid.uuid4()}@example.com")
    return user["id"]


def _mk_job(store, user_id, mode="streamer", s3_input_key=None):
    job_id = str(uuid.uuid4())
    store.create_job(
        job_id=job_id, user_id=user_id, mode=mode, params={},
        s3_input_key=s3_input_key or f"users/{user_id}/jobs/{job_id}/{mode}/input/original.mp4",
    )
    return job_id


def _segments(n=3):
    return [
        {
            "start_sec": 10.0 * (i + 1),
            "duration_sec": 5.5 + i,
            "title": f"Segment {i}",
            "score": float(n - i),
        }
        for i in range(n)
    ]


# ── 1/2/3: create, nullable analysis_job_id, attach later ──────────────

def test_create_batch(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={"autoReviewMode": "manual"})

    assert batch["user_id"] == user_id
    assert batch["status"] == store.STREAMER_BATCH_STATUS_QUEUED
    assert batch["preset_snapshot"] == {"autoReviewMode": "manual"}


def test_analysis_job_id_can_be_null_at_creation(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    assert batch["analysis_job_id"] is None

    fetched = store.get_streamer_batch(batch["id"], user_id)
    assert fetched["analysis_job_id"] is None


def test_analysis_job_id_can_be_attached_later(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    job_id = _mk_job(store, user_id)

    store.attach_analysis_job_to_batch(batch["id"], job_id)

    fetched = store.get_streamer_batch(batch["id"], user_id)
    assert fetched["analysis_job_id"] == job_id


def test_analysis_job_id_can_be_set_at_creation_too(store, user_id):
    job_id = _mk_job(store, user_id)
    batch = store.create_streamer_batch(user_id, preset_snapshot={}, analysis_job_id=job_id)
    assert batch["analysis_job_id"] == job_id


# ── 4: batch belongs to user ────────────────────────────────────────────

def test_batch_not_visible_to_a_different_user(store, user_id):
    from scripts import auth_store
    other_user = auth_store.create_user(f"other-{uuid.uuid4()}@example.com")["id"]

    batch = store.create_streamer_batch(user_id, preset_snapshot={})

    assert store.get_streamer_batch(batch["id"], other_user) is None
    assert store.get_streamer_batch(batch["id"], user_id) is not None


def test_list_user_streamer_batches_scoped_to_user(store, user_id):
    from scripts import auth_store
    other_user = auth_store.create_user(f"other-{uuid.uuid4()}@example.com")["id"]

    store.create_streamer_batch(user_id, preset_snapshot={})
    store.create_streamer_batch(other_user, preset_snapshot={})

    mine = store.list_user_streamer_batches(user_id)
    assert all(b["user_id"] == user_id for b in mine)
    assert len(mine) >= 1


# ── 5/6: segment order + start_sec/duration_sec preserved exactly ──────

def test_segment_order_is_preserved(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    segs = _segments(4)

    store.replace_streamer_segments(batch["id"], segs)
    listed = store.list_streamer_segments(batch["id"])

    assert [s["title"] for s in listed] == [s["title"] for s in segs]
    assert [s["ordinal"] for s in listed] == [0, 1, 2, 3]


def test_start_and_duration_saved_without_transformation(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    segs = [{"start_sec": 4350.25, "duration_sec": 372.5, "title": "T"}]

    store.replace_streamer_segments(batch["id"], segs)
    listed = store.list_streamer_segments(batch["id"])

    assert listed[0]["start_sec"] == 4350.25
    assert listed[0]["duration_sec"] == 372.5


# ── 7: replace leaves no orphans ────────────────────────────────────────

def test_replace_segments_leaves_no_orphan_rows(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(5))
    assert len(store.list_streamer_segments(batch["id"])) == 5

    store.replace_streamer_segments(batch["id"], _segments(2))
    remaining = store.list_streamer_segments(batch["id"])

    assert len(remaining) == 2
    assert [s["ordinal"] for s in remaining] == [0, 1]


def test_replace_segments_rejected_once_a_clip_job_is_attached(store, user_id):
    """Hardening: replace_streamer_segments() must refuse outright once any
    streamer_clip_jobs row exists for the batch, not just document the
    risk — old segments, the clip job, and the real generation_jobs row
    must all survive the rejected call untouched."""
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(2))
    original_segment_ids = {s["id"] for s in store.list_streamer_segments(batch["id"])}
    seg = store.list_streamer_segments(batch["id"])[0]
    job_id = _mk_job(store, user_id)
    store.attach_streamer_clip_job(batch["id"], seg["id"], job_id)

    with pytest.raises(store.StreamerSegmentsLockedError):
        store.replace_streamer_segments(batch["id"], _segments(5))

    # Old segments untouched — exact same rows, not replaced/deleted.
    remaining_segments = store.list_streamer_segments(batch["id"])
    assert {s["id"] for s in remaining_segments} == original_segment_ids

    # The clip job link and the real generation_jobs row both survive.
    assert len(store.list_streamer_clip_jobs(batch["id"])) == 1
    assert store.get_job(job_id) is not None


# ── 8: selected state ───────────────────────────────────────────────────

def test_selected_defaults_true_and_can_be_toggled(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(1))
    seg = store.list_streamer_segments(batch["id"])[0]
    assert seg["selected"] is True

    store.set_streamer_segment_selection(seg["id"], False)
    updated = store.list_streamer_segments(batch["id"])[0]
    assert updated["selected"] is False


# ── 9/10: clip job attachment + duplicate handling ──────────────────────

def test_attach_streamer_clip_job(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(1))
    seg = store.list_streamer_segments(batch["id"])[0]
    job_id = _mk_job(store, user_id)

    link = store.attach_streamer_clip_job(batch["id"], seg["id"], job_id)

    assert link["segment_id"] == seg["id"]
    assert link["job_id"] == job_id
    assert store.list_streamer_clip_jobs(batch["id"])[0]["job_id"] == job_id


def test_reattaching_same_job_to_same_segment_is_idempotent(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(1))
    seg = store.list_streamer_segments(batch["id"])[0]
    job_id = _mk_job(store, user_id)

    first = store.attach_streamer_clip_job(batch["id"], seg["id"], job_id)
    second = store.attach_streamer_clip_job(batch["id"], seg["id"], job_id)

    assert first["job_id"] == second["job_id"]
    assert len(store.list_streamer_clip_jobs(batch["id"])) == 1  # not duplicated


def test_attaching_a_different_job_to_an_already_linked_segment_conflicts(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(1))
    seg = store.list_streamer_segments(batch["id"])[0]
    job_a = _mk_job(store, user_id)
    job_b = _mk_job(store, user_id)

    store.attach_streamer_clip_job(batch["id"], seg["id"], job_a)

    with pytest.raises(store.StreamerClipJobConflictError):
        store.attach_streamer_clip_job(batch["id"], seg["id"], job_b)


def test_same_job_cannot_be_attached_to_two_segments(store, user_id):
    """job_id UNIQUE on streamer_clip_jobs -- one job belongs to at most
    one segment record."""
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(2))
    seg_a, seg_b = store.list_streamer_segments(batch["id"])
    job_id = _mk_job(store, user_id)

    store.attach_streamer_clip_job(batch["id"], seg_a["id"], job_id)
    with pytest.raises(Exception):  # psycopg2 IntegrityError on the UNIQUE(job_id)
        store.attach_streamer_clip_job(batch["id"], seg_b["id"], job_id)


def test_deleting_batch_cascades_segments_and_clip_jobs_but_not_generation_jobs(store, user_id):
    from scripts.prod_job_store import _get_conn

    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.replace_streamer_segments(batch["id"], _segments(1))
    seg = store.list_streamer_segments(batch["id"])[0]
    job_id = _mk_job(store, user_id)
    store.attach_streamer_clip_job(batch["id"], seg["id"], job_id)

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM streamer_batches WHERE id=%s", (batch["id"],))
    finally:
        conn.close()

    assert store.list_streamer_segments(batch["id"]) == []
    assert store.list_streamer_clip_jobs(batch["id"]) == []
    # The real generation_jobs row must survive the batch's deletion.
    assert store.get_job(job_id) is not None


# ── 11: telegram_notified_at ─────────────────────────────────────────────

def test_telegram_notified_at_initially_null(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    assert batch["telegram_notified_at"] is None


# ── 12: terminal status doesn't roll back ───────────────────────────────

def test_terminal_status_cannot_transition_to_a_different_status(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_READY)

    with pytest.raises(store.StreamerBatchTransitionError):
        store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_ANALYZING)


def test_failed_cannot_transition_to_generating(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_FAILED, error="boom")

    with pytest.raises(store.StreamerBatchTransitionError):
        store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_GENERATING)


def test_setting_the_same_terminal_status_again_is_idempotent(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    first = store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_READY)
    second = store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_READY)

    assert first["completed_at"] == second["completed_at"]  # not bumped on replay


def test_concurrent_terminal_transitions_never_silently_overwrite_each_other(store, user_id):
    """
    Concurrency-oriented hardening regression for the atomic-UPDATE guard:
    many threads racing to move the SAME non-terminal batch to two
    DIFFERENT terminal statuses at once.

    With the old SELECT-then-UPDATE implementation this was a real race:
    two threads could both read the pre-transition (non-terminal) status
    before either had committed, both pass the guard, and both UPDATE —
    the second commit silently overwriting the first with no error ever
    raised, for two genuinely different terminal outcomes. The single
    atomic conditional UPDATE closes that window: at most one distinct
    terminal status can ever actually land, and every attempt targeting
    the other one must observe StreamerBatchTransitionError, never a
    silent overwrite.
    """
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_GENERATING)

    targets = (
        [store.STREAMER_BATCH_STATUS_READY] * 8
        + [store.STREAMER_BATCH_STATUS_FAILED] * 8
    )

    def attempt(target):
        try:
            row = store.set_streamer_batch_status(
                batch["id"], target,
                error=None if target == store.STREAMER_BATCH_STATUS_READY else "boom",
            )
            return ("ok", row["status"])
        except store.StreamerBatchTransitionError:
            return ("blocked", target)

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(attempt, targets))

    final = store.get_streamer_batch(batch["id"], user_id)
    succeeded_statuses = {status for outcome, status in results if outcome == "ok"}
    blocked_statuses = {status for outcome, status in results if outcome == "blocked"}

    # Exactly one distinct terminal status actually landed, and it's the
    # one every "ok" result agrees on and matches the persisted row.
    assert succeeded_statuses == {final["status"]}
    # Every attempt targeting the OTHER terminal status was cleanly
    # blocked -- never silently applied and then clobbered by the winner.
    other = (
        store.STREAMER_BATCH_STATUS_FAILED
        if final["status"] == store.STREAMER_BATCH_STATUS_READY
        else store.STREAMER_BATCH_STATUS_READY
    )
    assert blocked_statuses == {other}


def test_non_terminal_transitions_are_unrestricted(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_ANALYZING)
    store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_AWAITING_SELECTION)
    result = store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_GENERATING)

    assert result["status"] == store.STREAMER_BATCH_STATUS_GENERATING
    assert result["completed_at"] is None  # non-terminal — never set


def test_completed_at_set_on_first_terminal_transition(store, user_id):
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    assert batch["completed_at"] is None

    result = store.set_streamer_batch_status(batch["id"], store.STREAMER_BATCH_STATUS_PARTIALLY_FAILED)
    assert result["completed_at"] is not None


# ── segments_from_analysis() store contract ─────────────────────────────

def test_segments_from_analysis_maps_analyze_output(store, user_id):
    analysis_result = {
        "segments": [
            {"start_sec": 1.0, "duration_sec": 10.0, "score": 2.0, "source": "speaker_fallback"},
            {"start_sec": 20.0, "duration_sec": 5.0, "score": 1.0, "source": "speaker_fallback"},
        ],
        "crop_hints": {"anchor": "center"},
    }

    mapped = store.segments_from_analysis(analysis_result, titles=["Intro", "Reaction"])

    assert mapped[0]["title"] == "Intro"
    assert mapped[1]["title"] == "Reaction"
    assert mapped[0]["start_sec"] == 1.0
    assert mapped[0]["crop_hints"] == {"anchor": "center"}
    assert all(m["recommended"] is False for m in mapped)


def test_segments_from_analysis_falls_back_to_placeholder_titles(store, user_id):
    analysis_result = {"segments": [{"start_sec": 0.0, "duration_sec": 5.0, "score": 1.0}]}
    mapped = store.segments_from_analysis(analysis_result)
    assert mapped[0]["title"] == "Тема 1"


def test_segments_from_analysis_persists_via_replace_streamer_segments(store, user_id):
    """The full round trip the store contract is meant for: analyze()-shaped
    dict -> segments_from_analysis() -> replace_streamer_segments()."""
    batch = store.create_streamer_batch(user_id, preset_snapshot={})
    analysis_result = {
        "segments": [{"start_sec": 3.0, "duration_sec": 12.0, "score": 5.0, "source": "x"}],
        "crop_hints": {"x": 1},
    }

    mapped = store.segments_from_analysis(analysis_result, titles=["Highlight"])
    store.replace_streamer_segments(batch["id"], mapped)

    listed = store.list_streamer_segments(batch["id"])
    assert listed[0]["title"] == "Highlight"
    assert listed[0]["start_sec"] == 3.0
    assert listed[0]["crop_hints"] == {"x": 1}


# ── 13: migration idempotent ────────────────────────────────────────────

def test_migration_012_is_idempotent(store):
    """Re-running migration 012's SQL text against an already-migrated
    connection must not raise — every CREATE/ALTER is IF NOT EXISTS,
    matching every migration before it."""
    from pathlib import Path
    from scripts.prod_job_store import _get_conn

    sql = (Path(__file__).parent.parent / "scripts/migrations/012_streamer_batches.sql").read_text()
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    finally:
        conn.close()
