"""Cross-process session turn lease behavior (#84234)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import hermes_state
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError


def test_turn_lease_serializes_separate_session_db_instances(tmp_path):
    """A second process-shaped DB handle waits for the current turn owner."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    second = SessionDB(path)
    first.create_session("shared", source="test")

    first_holder = f"pid={os.getpid()}:turn=first"
    second_holder = f"pid={os.getpid()}:turn=second"
    assert first.try_acquire_session_turn_lease(
        "shared", first_holder, ttl_seconds=5
    )

    released = threading.Event()

    def release_first():
        time.sleep(0.2)
        first.release_session_turn_lease("shared", first_holder)
        released.set()

    thread = threading.Thread(target=release_first, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        assert second.acquire_session_turn_lease(
            "shared",
            second_holder,
            ttl_seconds=5,
            wait_seconds=2,
            poll_interval_seconds=0.02,
        )
    finally:
        thread.join(timeout=2)

    assert released.is_set()
    assert time.monotonic() - started >= 0.15
    second.release_session_turn_lease("shared", second_holder)


def test_turn_lease_is_scoped_to_conversation_root(tmp_path):
    """Compression descendants share one durable serialization domain."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")

    root_holder = f"pid={os.getpid()}:turn=root"
    child_holder = f"pid={os.getpid()}:turn=child"
    assert db.try_acquire_session_turn_lease(
        "root", root_holder, ttl_seconds=5
    )
    assert not db.try_acquire_session_turn_lease(
        "child", child_holder, ttl_seconds=5
    )
    db.release_session_turn_lease("child", root_holder)


def test_turn_lease_does_not_serialize_delegate_child_with_parent(tmp_path):
    """Only compression continuation segments share a conversation lease."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="test")
    db.create_session(
        "delegate",
        source="delegate",
        parent_session_id="parent",
        model_config={"_delegate_from": "parent"},
    )

    parent_holder = f"pid={os.getpid()}:turn=parent"
    delegate_holder = f"pid={os.getpid()}:turn=delegate"
    assert db.try_acquire_session_turn_lease(
        "parent", parent_holder, ttl_seconds=5
    )
    assert db.try_acquire_session_turn_lease(
        "delegate", delegate_holder, ttl_seconds=5
    )


def test_turn_lease_walks_compression_child_that_inherited_fork_markers(tmp_path):
    """Inherited ``_delegate_from`` / ``_branched_from`` must not stop the walk.

    ``publish_compression_child`` copies ``model_config`` verbatim, so a
    delegate or branch continuation carries a marker pointing at some other
    session. Presence-only fork detection would key the child separately:
    the holder still owns the parent-key lease, but the first refresh after
    rotation looks up the child id and fail-closes with a hard interrupt.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("original-parent", source="test")
    db.create_session(
        "delegate",
        source="delegate",
        parent_session_id="original-parent",
        model_config={"_delegate_from": "original-parent"},
    )
    db.end_session("delegate", "compression")
    db.create_session(
        "delegate-continuation",
        source="delegate",
        parent_session_id="delegate",
        model_config={"_delegate_from": "original-parent"},
    )
    db.create_session(
        "branch",
        source="test",
        parent_session_id="original-parent",
        model_config={"_branched_from": "original-parent"},
    )
    db.end_session("branch", "compression")
    db.create_session(
        "branch-continuation",
        source="test",
        parent_session_id="branch",
        model_config={"_branched_from": "original-parent"},
    )

    assert db._session_turn_lease_key("delegate-continuation") == "delegate"
    assert db._session_turn_lease_key("branch-continuation") == "branch"

    delegate_holder = f"pid={os.getpid()}:turn=delegate"
    assert db.try_acquire_session_turn_lease(
        "delegate", delegate_holder, ttl_seconds=5
    )
    assert not db.try_acquire_session_turn_lease(
        "delegate-continuation",
        f"pid={os.getpid()}:turn=delegate-child",
        ttl_seconds=5,
    )
    assert db.refresh_session_turn_lease(
        "delegate-continuation", delegate_holder, ttl_seconds=5
    )

    branch_holder = f"pid={os.getpid()}:turn=branch"
    assert db.try_acquire_session_turn_lease(
        "branch", branch_holder, ttl_seconds=5
    )
    assert not db.try_acquire_session_turn_lease(
        "branch-continuation",
        f"pid={os.getpid()}:turn=branch-child",
        ttl_seconds=5,
    )
    assert db.refresh_session_turn_lease(
        "branch-continuation", branch_holder, ttl_seconds=5
    )

    original_holder = f"pid={os.getpid()}:turn=original"
    assert db.try_acquire_session_turn_lease(
        "original-parent", original_holder, ttl_seconds=5
    )
    db.release_session_turn_lease("delegate-continuation", delegate_holder)
    db.release_session_turn_lease("branch-continuation", branch_holder)
    db.release_session_turn_lease("original-parent", original_holder)


def test_turn_lease_write_txn_does_not_trust_fail_open_key_helper(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Acquire/refresh/release walk inside the write txn.

    The old helper swallowed get_session failures and returned the child id.
    P2 then proceeded to acquire; the write succeeded under that child key
    and the first working refresh walked to the parent and hard-interrupted.
    Poisoning the outer helper must not change the conversation key.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session(
        "delegate",
        source="delegate",
        model_config={"_delegate_from": "original-parent"},
    )
    db.end_session("delegate", "compression")
    db.create_session(
        "delegate-continuation",
        source="delegate",
        parent_session_id="delegate",
        model_config={"_delegate_from": "original-parent"},
    )

    monkeypatch.setattr(db, "_session_turn_lease_key", lambda sid: sid)
    holder = f"pid={os.getpid()}:turn=delegate"
    assert db.try_acquire_session_turn_lease(
        "delegate", holder, ttl_seconds=5
    )
    assert not db.try_acquire_session_turn_lease(
        "delegate-continuation",
        f"pid={os.getpid()}:turn=child",
        ttl_seconds=5,
    )
    assert db.refresh_session_turn_lease(
        "delegate-continuation", holder, ttl_seconds=5
    )
    db.release_session_turn_lease("delegate-continuation", holder)
    assert db.try_acquire_session_turn_lease(
        "delegate", f"pid={os.getpid()}:turn=next", ttl_seconds=5
    )


def test_turn_lease_retries_locked_in_txn_key_walk(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A locked lineage walk must retry, not INSERT under the child id."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session(
        "delegate",
        source="delegate",
        model_config={"_delegate_from": "original-parent"},
    )
    db.end_session("delegate", "compression")
    db.create_session(
        "delegate-continuation",
        source="delegate",
        parent_session_id="delegate",
        model_config={"_delegate_from": "original-parent"},
    )

    attempts = {"n": 0}
    original = db._session_turn_lease_key_on_conn

    def flaky_walk(conn, session_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(conn, session_id)

    monkeypatch.setattr(db, "_session_turn_lease_key_on_conn", flaky_walk)
    holder = f"pid={os.getpid()}:turn=delegate"
    assert db.try_acquire_session_turn_lease(
        "delegate-continuation", holder, ttl_seconds=5
    )
    assert attempts["n"] >= 2
    monkeypatch.setattr(db, "_session_turn_lease_key_on_conn", original)
    assert not db.try_acquire_session_turn_lease(
        "delegate", f"pid={os.getpid()}:turn=other", ttl_seconds=5
    )
    assert db.refresh_session_turn_lease("delegate", holder, ttl_seconds=5)
    db.release_session_turn_lease("delegate-continuation", holder)


def test_turn_lease_refresh_and_release_are_owner_fenced(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")

    current_holder = f"pid={os.getpid()}:turn=current"
    stale_holder = f"pid={os.getpid()}:turn=stale"
    next_holder = f"pid={os.getpid()}:turn=next"
    assert db.try_acquire_session_turn_lease(
        "shared", current_holder, ttl_seconds=5
    )
    assert not db.refresh_session_turn_lease(
        "shared", stale_holder, ttl_seconds=5
    )
    db.release_session_turn_lease("shared", stale_holder)
    assert not db.try_acquire_session_turn_lease(
        "shared", next_holder, ttl_seconds=5
    )

    assert db.refresh_session_turn_lease(
        "shared", current_holder, ttl_seconds=5
    )
    db.release_session_turn_lease("shared", current_holder)
    assert db.try_acquire_session_turn_lease(
        "shared", next_holder, ttl_seconds=5
    )


def test_expired_turn_lease_is_reclaimed(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    assert db.try_acquire_session_turn_lease(
        "shared", "legacy-holder", ttl_seconds=0.05
    )

    time.sleep(0.15)

    assert db.try_acquire_session_turn_lease(
        "shared", "pid=202:turn=reclaimer", ttl_seconds=5
    )


def test_acquire_turn_lease_notifies_wait_callback(tmp_path):
    """Waiters get a progress callback while another holder owns the lease."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    second = SessionDB(path)
    first.create_session("shared", source="test")

    first_holder = f"pid={os.getpid()}:turn=first"
    second_holder = f"pid={os.getpid()}:turn=second"
    assert first.try_acquire_session_turn_lease(
        "shared", first_holder, ttl_seconds=5
    )

    notices = []

    def release_first():
        time.sleep(0.12)
        first.release_session_turn_lease("shared", first_holder)

    thread = threading.Thread(target=release_first, daemon=True)
    thread.start()
    try:
        assert second.acquire_session_turn_lease(
            "shared",
            second_holder,
            ttl_seconds=5,
            wait_seconds=2,
            poll_interval_seconds=0.02,
            on_wait=notices.append,
            wait_notice_interval_seconds=0.05,
        )
    finally:
        thread.join(timeout=2)

    assert notices
    assert notices[0] < 0.05
    second.release_session_turn_lease("shared", second_holder)


def test_acquire_turn_lease_honors_should_abort(tmp_path):
    """Waiters stop immediately when should_abort() returns True."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    second = SessionDB(path)
    first.create_session("shared", source="test")

    first_holder = f"pid={os.getpid()}:turn=first"
    second_holder = f"pid={os.getpid()}:turn=second"
    assert first.try_acquire_session_turn_lease(
        "shared", first_holder, ttl_seconds=60
    )

    abort_checks = {"count": 0}

    def should_abort():
        abort_checks["count"] += 1
        return True

    started = time.monotonic()
    assert not second.acquire_session_turn_lease(
        "shared",
        second_holder,
        wait_seconds=30,
        poll_interval_seconds=0.05,
        should_abort=should_abort,
    )
    assert time.monotonic() - started < 1.0
    assert abort_checks["count"] >= 1
    first.release_session_turn_lease("shared", first_holder)


def test_acquire_turn_lease_retries_sqlite_lock(tmp_path, monkeypatch):
    """Write-lock exhaustion is retried, not a hard abort of the wait, and is not a lease wait:
    it reports on_contended instead of on_wait and does not sit out a poll interval on top of
    the patience it already spent."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=waiter"
    attempts = {"n": 0}
    original = db.try_acquire_session_turn_lease

    def flaky_acquire(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError(
                "database is locked (another Hermes process held the "
                "state.db write lock for over 20s)"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "try_acquire_session_turn_lease", flaky_acquire)
    notices, contended = [], []
    started = time.monotonic()
    assert db.acquire_session_turn_lease(
        "shared",
        holder,
        wait_seconds=10,
        poll_interval_seconds=5,
        on_wait=notices.append,
        on_contended=lambda: contended.append(True),
        acquire_patience_s=0.05,
    )
    assert time.monotonic() - started < 2.0
    assert (notices, contended) == ([], [True])
    assert attempts["n"] >= 2
    db.release_session_turn_lease("shared", holder)


def test_zero_wait_acquire_rides_out_sqlite_lock(tmp_path, monkeypatch):
    """Desktop/TUI pass wait_seconds=0 (no waiting on a holder). A busy write lock is not a
    holder: with a contended budget the retry wins the lease instead of failing as 'another
    active writer' (2026-10-28, chat 20261025_234720_c405df)."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=desktop"
    attempts = {"n": 0}
    original = db.try_acquire_session_turn_lease

    def flaky_acquire(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "try_acquire_session_turn_lease", flaky_acquire)
    assert db.acquire_session_turn_lease(
        "shared", holder, wait_seconds=0.0, contended_wait_seconds=5.0, acquire_patience_s=0.05,
    )
    assert attempts["n"] >= 2
    db.release_session_turn_lease("shared", holder)


def test_zero_wait_acquire_still_fails_fast_on_a_real_holder(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    other = f"pid={os.getpid()}:turn=other"
    assert db.try_acquire_session_turn_lease("shared", other, ttl_seconds=60)
    started = time.monotonic()
    assert not db.acquire_session_turn_lease(
        "shared", f"pid={os.getpid()}:turn=desktop", wait_seconds=0.0, contended_wait_seconds=5.0,
    )
    assert time.monotonic() - started < 1.0
    db.release_session_turn_lease("shared", other)


def test_acquire_turn_lease_locked_for_the_whole_wait_times_out(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "try_acquire_session_turn_lease", locked)
    notices = []
    assert not db.acquire_session_turn_lease(
        "shared", f"pid={os.getpid()}:turn=waiter", wait_seconds=0.3, poll_interval_seconds=5,
        on_wait=notices.append,
    )
    assert notices == []


def test_acquire_turn_lease_reraises_non_lock_sqlite_error(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")

    def disk_full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(db, "try_acquire_session_turn_lease", disk_full)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        db.acquire_session_turn_lease(
            "shared",
            f"pid={os.getpid()}:turn=waiter",
            wait_seconds=1,
            poll_interval_seconds=0.02,
        )


def test_non_expired_turn_lease_from_dead_pid_is_reclaimed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder whose structured pid= no longer exists can be reclaimed early.

    Stamped with this process's own PID namespace: same-namespace kernel proof
    is still proof (a foreign or unstamped holder would defer to TTL instead —
    see tests/hermes_state/test_lease_pid_namespace.py)."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")

    from hermes_state_pidns import holder_namespace_token
    dead_holder = f"pid=424242{holder_namespace_token()}:turn=dead:platform=test"
    assert db.try_acquire_session_turn_lease(
        "shared", dead_holder, ttl_seconds=300
    ) is True

    probed: list[int] = []

    def pid_exists(pid: int) -> bool:
        probed.append(pid)
        return False

    monkeypatch.setattr(
        hermes_state, "psutil", SimpleNamespace(pid_exists=pid_exists)
    )

    fresh_holder = "pid=525252:turn=fresh:platform=test"
    assert db.try_acquire_session_turn_lease(
        "shared", fresh_holder, ttl_seconds=300
    ) is True
    assert probed == [424242]


def test_turn_lease_fences_stale_transcript_flush_after_reclaim(tmp_path):
    """A lost holder cannot persist after B has taken the conversation.

    Refresh-loss interrupt is cooperative; the lease itself must reject the
    late append inside the same SQLite write transaction.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    stale_holder = f"pid={os.getpid()}:turn=stale"
    next_holder = f"pid={os.getpid()}:turn=next"

    assert db.try_acquire_session_turn_lease(
        "shared", stale_holder, ttl_seconds=5
    )
    assert db.append_messages_batch(
        "shared",
        [{"role": "user", "content": "stale-owned"}],
        turn_lease_holder=stale_holder,
    ) == 1

    db.release_session_turn_lease("shared", stale_holder)
    assert db.try_acquire_session_turn_lease(
        "shared", next_holder, ttl_seconds=5
    )

    with pytest.raises(SessionTurnLeaseLostError, match="turn lease lost"):
        db.append_messages_batch(
            "shared",
            [{"role": "assistant", "content": "late stale reply"}],
            turn_lease_holder=stale_holder,
        )
    with pytest.raises(SessionTurnLeaseLostError, match="turn lease lost"):
        db.append_message(
            "shared",
            "assistant",
            "late stale single-row",
            turn_lease_holder=stale_holder,
        )

    assert db.append_messages_batch(
        "shared",
        [{"role": "assistant", "content": "next reply"}],
        turn_lease_holder=next_holder,
    ) == 1
    assert [m["content"] for m in db.get_messages("shared")] == [
        "stale-owned",
        "next reply",
    ]
    db.release_session_turn_lease("shared", next_holder)


def test_turn_lease_reclaims_missing_row_for_matching_holder(tmp_path):
    """A still-running writer can persist if the SQLite lease row vanished.

    ``_check_transcript_write_guards`` used to treat missing-or-mismatch as one
    error; the UI glossed that as takeover even when no other holder existed.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=owner"
    other = f"pid={os.getpid()}:turn=other"
    assert db.try_acquire_session_turn_lease("shared", holder, ttl_seconds=5)
    raw = sqlite3.connect(tmp_path / "state.db")
    with raw:
        raw.execute("DELETE FROM session_turn_leases WHERE conversation_id = 'shared'")
    assert db.append_messages_batch(
        "shared",
        [{"role": "assistant", "content": "reclaimed"}],
        turn_lease_holder=holder,
    ) == 1
    with db._read_ctx() as conn:
        lease = conn.execute(
            "SELECT holder FROM session_turn_leases WHERE conversation_id = ?",
            ("shared",),
        ).fetchone()
    assert lease["holder"] == holder
    assert [m["content"] for m in db.get_messages("shared")] == ["reclaimed"]
    assert db.try_acquire_session_turn_lease("shared", other, ttl_seconds=5) is False
    with pytest.raises(SessionTurnLeaseLostError, match="held by"):
        db.append_messages_batch(
            "shared",
            [{"role": "assistant", "content": "late"}],
            turn_lease_holder=other,
        )
    raw.close()
    db.release_session_turn_lease("shared", holder)


def test_turn_lease_revives_expired_row_still_owned_by_writer(tmp_path, monkeypatch):
    import hermes_state_compression
    import hermes_state_messages

    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=owner"
    contender = f"pid={os.getpid()}:turn=contender"

    # Control both lease clocks, not the shared time module: a descheduled
    # runner must not expire the renewed lease before the contender checks it.
    now = 1000.0
    clock = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(hermes_state_compression, "time", clock)
    monkeypatch.setattr(hermes_state_messages, "time", clock)
    assert db.try_acquire_session_turn_lease("shared", holder, ttl_seconds=0.05)
    now += 0.12  # Acquisition floors the TTL at 0.1 seconds.
    assert db.append_messages_batch(
        "shared",
        [{"role": "assistant", "content": "after ttl"}],
        turn_lease_holder=holder,
        turn_lease_ttl_seconds=0.2,
    ) == 1
    with db._read_ctx() as conn:
        lease = conn.execute(
            "SELECT holder, expires_at FROM session_turn_leases WHERE conversation_id = ?",
            ("shared",),
        ).fetchone()
    assert lease["holder"] == holder
    assert lease["expires_at"] == pytest.approx(now + 0.2)
    assert not db.try_acquire_session_turn_lease("shared", contender, ttl_seconds=5)

    now += 0.21
    assert db.try_acquire_session_turn_lease("shared", contender, ttl_seconds=5)


def test_turn_lease_acquisition_sweeps_long_expired_rows_of_other_conversations(tmp_path):
    """A holder that died without releasing leaves its row behind; the next acquisition anywhere
    removes rows expired past the grace, and keeps recent expiries their live owner may renew."""
    db = SessionDB(tmp_path / "state.db")
    for sid in ("abandoned", "recent", "within-grace", "fresh"):
        db.create_session(sid, source="test")
    raw = sqlite3.connect(tmp_path / "state.db")
    with raw:
        raw.executemany(
            "INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            [("abandoned", "pid=1:turn=crashed", time.time() - 86400 - 120, time.time() - 86400 - 60),
             ("recent", f"pid={os.getpid()}:turn=starved", time.time() - 60, time.time() - 10),
             ("within-grace", "pid=1:turn=suspended", time.time() - 86400, time.time() - 86400 + 60)])

    assert db.try_acquire_session_turn_lease("fresh", f"pid={os.getpid()}:turn=next", ttl_seconds=5)

    rows = dict(raw.execute("SELECT conversation_id, holder FROM session_turn_leases").fetchall())
    raw.close()
    assert rows == {"recent": f"pid={os.getpid()}:turn=starved",
                    "within-grace": "pid=1:turn=suspended",
                    "fresh": f"pid={os.getpid()}:turn=next"}
    assert db.append_messages_batch(
        "recent", [{"role": "assistant", "content": "after a starved refresher"}],
        turn_lease_holder=f"pid={os.getpid()}:turn=starved", turn_lease_ttl_seconds=5,
    ) == 1


def test_turn_lease_fences_flush_when_row_is_absent(tmp_path):
    """A vacant conversation is reclaimed by the presented holder, not treated as takeover."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=owner"
    other = f"pid={os.getpid()}:turn=other"

    assert db.append_messages_batch(
        "shared",
        [{"role": "assistant", "content": "after release"}],
        turn_lease_holder=holder,
    ) == 1
    assert [m["content"] for m in db.get_messages("shared")] == ["after release"]
    with pytest.raises(SessionTurnLeaseLostError, match="held by"):
        db.append_messages_batch(
            "shared",
            [{"role": "assistant", "content": "late"}],
            turn_lease_holder=other,
        )
    db.release_session_turn_lease("shared", holder)


def test_turn_lease_fence_walks_compression_child_to_root(tmp_path):
    """A parent-key holder still fences writes against the rotated tip."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="test")
    db.end_session("root", "compression")
    db.create_session("child", source="test", parent_session_id="root")

    root_holder = f"pid={os.getpid()}:turn=root"
    stale_holder = f"pid={os.getpid()}:turn=stale"
    assert db.try_acquire_session_turn_lease(
        "root", root_holder, ttl_seconds=5
    )
    assert db.append_messages_batch(
        "child",
        [{"role": "user", "content": "owner on tip"}],
        turn_lease_holder=root_holder,
    ) == 1
    with pytest.raises(SessionTurnLeaseLostError, match="turn lease lost"):
        db.append_messages_batch(
            "child",
            [{"role": "assistant", "content": "impostor"}],
            turn_lease_holder=stale_holder,
        )
    db.release_session_turn_lease("child", root_holder)


def test_lost_turn_lease_flush_fails_fast_without_patience_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Sibling of test_a_lost_compression_lease_still_fails_fast.

    SessionTurnLeaseLostError is permanent fencing, not a live-busy signal.
    Retrying it would burn transcript write patience and still fail.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    stale_holder = f"pid={os.getpid()}:turn=stale"
    next_holder = f"pid={os.getpid()}:turn=next"
    assert db.try_acquire_session_turn_lease(
        "shared", stale_holder, ttl_seconds=5
    )
    db.release_session_turn_lease("shared", stale_holder)
    assert db.try_acquire_session_turn_lease(
        "shared", next_holder, ttl_seconds=5
    )

    sleeps = []
    original = db._sleep_before_write_retry

    def track_sleep(deadline, patience_s):
        sleeps.append(patience_s)
        return original(deadline, patience_s)

    monkeypatch.setattr(db, "_sleep_before_write_retry", track_sleep)
    monkeypatch.setattr(SessionDB, "_COMPRESSION_BUSY_WAIT_S", 5.0)

    started = time.monotonic()
    with pytest.raises(SessionTurnLeaseLostError, match="turn lease lost"):
        db.append_messages_batch(
            "shared",
            [{"role": "assistant", "content": "late stale reply"}],
            turn_lease_holder=stale_holder,
        )
    assert time.monotonic() - started < 0.5
    assert sleeps == []
    assert db.get_messages("shared") == []
    db.release_session_turn_lease("shared", next_holder)


def test_turn_lease_fence_walks_continuation_that_inherited_fork_markers(tmp_path):
    """Owner flush on a rotated tip must use the parent-key lease.

    Presence-only ``_delegate_from`` / ``_branched_from`` detection would
    treat the continuation as its own conversation. The presented parent
    holder would then miss the row and fail-close a still-valid owner.
    """
    db = SessionDB(tmp_path / "state.db")
    db.create_session("original-parent", source="test")
    db.create_session(
        "delegate",
        source="delegate",
        parent_session_id="original-parent",
        model_config={"_delegate_from": "original-parent"},
    )
    db.end_session("delegate", "compression")
    db.create_session(
        "delegate-continuation",
        source="delegate",
        parent_session_id="delegate",
        model_config={"_delegate_from": "original-parent"},
    )
    db.create_session(
        "branch",
        source="test",
        parent_session_id="original-parent",
        model_config={"_branched_from": "original-parent"},
    )
    db.end_session("branch", "compression")
    db.create_session(
        "branch-continuation",
        source="test",
        parent_session_id="branch",
        model_config={"_branched_from": "original-parent"},
    )

    delegate_holder = f"pid={os.getpid()}:turn=delegate"
    assert db.try_acquire_session_turn_lease(
        "delegate", delegate_holder, ttl_seconds=5
    )
    assert db.append_messages_batch(
        "delegate-continuation",
        [{"role": "user", "content": "owner on inherited tip"}],
        turn_lease_holder=delegate_holder,
    ) == 1
    with pytest.raises(SessionTurnLeaseLostError, match="turn lease lost"):
        db.append_messages_batch(
            "delegate-continuation",
            [{"role": "assistant", "content": "impostor"}],
            turn_lease_holder=f"pid={os.getpid()}:turn=impostor",
        )

    branch_holder = f"pid={os.getpid()}:turn=branch"
    assert db.try_acquire_session_turn_lease(
        "branch", branch_holder, ttl_seconds=5
    )
    assert db.append_messages_batch(
        "branch-continuation",
        [{"role": "user", "content": "branch owner on inherited tip"}],
        turn_lease_holder=branch_holder,
    ) == 1
    with pytest.raises(SessionTurnLeaseLostError, match="turn lease lost"):
        db.append_messages_batch(
            "branch-continuation",
            [{"role": "assistant", "content": "branch impostor"}],
            turn_lease_holder=f"pid={os.getpid()}:turn=branch-impostor",
        )

    assert [m["content"] for m in db.get_messages("delegate-continuation")] == [
        "owner on inherited tip"
    ]
    assert [m["content"] for m in db.get_messages("branch-continuation")] == [
        "branch owner on inherited tip"
    ]
    db.release_session_turn_lease("delegate-continuation", delegate_holder)
    db.release_session_turn_lease("branch-continuation", branch_holder)


# --- A release that hit a busy state.db must not look like a live turn (agent-db9dfa2d4e) ---


class _HeldWriteLock:
    """Another connection holding the state.db write lock (BEGIN IMMEDIATE) until released."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
        self.conn.execute("BEGIN IMMEDIATE")

    def release(self):
        if self.conn is not None:
            self.conn.execute("COMMIT")
            self.conn.close()
            self.conn = None


def _lease_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return [tuple(r) for r in conn.execute("SELECT conversation_id, holder FROM session_turn_leases")]
    finally:
        conn.close()


def _locked_release(db, path, session_id, holder):
    """Release ``holder`` while another connection holds the write lock; return the lock."""
    db._WRITE_PATIENCE_S = 0.2
    lock = _HeldWriteLock(path)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.release_session_turn_lease(session_id, holder)
    return lock


def test_locked_release_frees_the_lease_for_the_next_same_process_turn(tmp_path):
    """A desktop turn (wait_seconds=0) after a lock-failed release in the same process is admitted."""
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    first = f"pid={os.getpid()}:turn=first:platform=desktop"
    second = f"pid={os.getpid()}:turn=second:platform=desktop"
    assert db.try_acquire_session_turn_lease("chat", first, ttl_seconds=300)

    lock = _locked_release(db, path, "chat", first)
    lock.release()
    db._WRITE_PATIENCE_S = SessionDB._WRITE_PATIENCE_S

    assert _lease_rows(path) == [("chat", first)]  # the failed DELETE left the row behind
    assert db.acquire_session_turn_lease("chat", second, ttl_seconds=300, wait_seconds=0)
    assert _lease_rows(path) == [("chat", second)]
    db.release_session_turn_lease("chat", second)


def test_live_same_process_lease_is_still_refused(tmp_path):
    """Only an abandoned holder is reclaimable early: a live same-process turn keeps its lease."""
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    live = f"pid={os.getpid()}:turn=live:platform=desktop"
    other = f"pid={os.getpid()}:turn=other:platform=desktop"
    assert db.try_acquire_session_turn_lease("chat", live, ttl_seconds=300)
    assert not db.acquire_session_turn_lease("chat", other, ttl_seconds=300, wait_seconds=0)
    # An abandoned holder on another db file does not leak into this one.
    other_path = tmp_path / "other.db"
    other_db = SessionDB(other_path)
    other_db.create_session("chat", source="desktop")
    assert other_db.try_acquire_session_turn_lease("chat", live, ttl_seconds=300)
    _locked_release(other_db, other_path, "chat", live).release()
    assert not db.try_acquire_session_turn_lease("chat", other, ttl_seconds=300)
    assert _lease_rows(path) == [("chat", live)]
    db.release_session_turn_lease("chat", live)


def test_facade_release_retries_a_locked_delete_in_the_background(tmp_path, caplog):
    """DurableTurnLease.release defers a locked DELETE: WARNING now, row gone once the lock clears,
    so another process (a second SessionDB) can take the lease without waiting out the TTL."""
    import logging

    from agent.turn_facade_lease import DurableTurnLease

    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=first:platform=desktop"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    agent = SimpleNamespace(_active_session_turn_lease_holder=holder, _active_session_turn_lease_ttl_seconds=300)
    lease = DurableTurnLease(agent, db, "chat", holder, expires_at=time.time() + 300)

    db._WRITE_PATIENCE_S = 0.2
    lock = _HeldWriteLock(path)
    with caplog.at_level(logging.WARNING):
        lease.release()  # must not block on the lock or raise
        assert agent._active_session_turn_lease_holder is None
        assert _lease_rows(path) == [("chat", holder)]
        time.sleep(0.5)
        lock.release()
        deadline = time.monotonic() + 10
        while _lease_rows(path) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert _lease_rows(path) == []
    assert any("lease release deferred" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    other_process = SessionDB(path)
    assert other_process.try_acquire_session_turn_lease(
        "chat", "pid=999999999:turn=elsewhere:platform=desktop", ttl_seconds=300)


def test_facade_release_logs_error_when_retries_run_out(tmp_path, caplog):
    """A DELETE that stays locked until the lease expires logs one ERROR (the detector's signal)."""
    import logging

    from agent.turn_facade_lease import DurableTurnLease

    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=stuck:platform=desktop"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    lease = DurableTurnLease(SimpleNamespace(), db, "chat", holder, expires_at=time.time() + 1.0)

    db._WRITE_PATIENCE_S = 0.2
    lock = _HeldWriteLock(path)
    try:
        with caplog.at_level(logging.WARNING):
            lease.release()
            deadline = time.monotonic() + 10
            while (not any("lease release retries exhausted" in r.getMessage() for r in caplog.records)
                   and time.monotonic() < deadline):
                time.sleep(0.05)
    finally:
        lock.release()
    exhausted = [r for r in caplog.records if "lease release retries exhausted" in r.getMessage()]
    assert len(exhausted) == 1 and exhausted[0].levelno == logging.ERROR
