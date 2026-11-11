"""Bounded claim age and recovery against isolated SQLite state."""
import contextlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_notify_clock as clock


@pytest.fixture
def claim(monkeypatch):
    ticks = [100.0]
    monkeypatch.setattr(notify.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(notify, "_my_holder", lambda: (111, 22))
    monkeypatch.setattr(notify, "_holder_alive", lambda row: True)
    with clock._lock:
        clock._started.clear()
    with contextlib.closing(kbc.connect()) as conn:
        task = kb.create_task(conn, title="Clock boundary", assignee="default")
        args = dict(task_id=task, platform="tui", chat_id="clock-test")
        notify.add_notify_sub(conn, **args)
        def event():
            with kb.write_txn(conn):
                kb._append_event(conn, task, "blocked", {})
            return conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
        event()
        old, end, events = notify.claim_unseen_events_for_sub(conn, **args, durable=True)
        assert events
        yield conn, args, ticks, event, old, end
    with clock._lock:
        clock._started.clear()


def test_fresh_own_claim_then_idle_expiry_ignores_wall_clock(claim, monkeypatch):
    conn, args, ticks, _, old, _ = claim
    monkeypatch.setattr(notify.time, "time", lambda: -1000000000)
    ticks[0] += notify.NOTIFY_CLAIM_STALE_S - 1
    assert notify.recover_orphaned_notify_claims(conn) == 0
    ticks[0] += 1
    assert notify.recover_orphaned_notify_claims(conn) == 1
    assert not notify.notify_claim_in_flight(conn, **args)
    assert notify._notify_cursor(conn, **args, thread_id=None) == old


def test_partial_ack_and_additional_claim_preserve_oldest_age(claim):
    conn, args, ticks, event, old, first = claim
    ticks[0] += 500
    end = event()
    assert notify.claim_unseen_events_for_sub(conn, **args, durable=True)[1] == end
    assert notify.ack_notify_claim(conn, **args, upto=first, from_cursor=old)
    assert clock._started
    ticks[0] = 100 + notify.NOTIFY_CLAIM_STALE_S
    assert notify.recover_orphaned_notify_claims(conn) == 1
    assert notify._notify_cursor(conn, **args, thread_id=None) == first


@pytest.mark.parametrize("finish", ["ack", "release", "rewind"])
def test_completed_marker_cleans_clock_and_new_claim_is_fresh(claim, finish):
    conn, args, ticks, event, old, end = claim
    ticks[0] += 800
    if finish == "ack":
        assert notify.ack_notify_claim(conn, **args, upto=end, from_cursor=old)
    elif finish == "release":
        assert notify.release_notify_claim(conn, **args)
    else:
        assert notify.rewind_notify_cursor(conn, **args, claimed_cursor=end, old_cursor=old)
    assert not clock._started
    event()
    notify.claim_unseen_events_for_sub(conn, **args, durable=True)
    ticks[0] += 101
    assert notify.recover_orphaned_notify_claims(conn) == 0


def test_prune_deleted_subscription_preserves_active_age(claim):
    conn, args, ticks, _, _, _ = claim
    active_key = notify._sub_key(**args, thread_id=None)
    row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (args["task_id"],)).fetchone()
    # A second cached subscription disappears outside the notification API.
    removed_key = ("removed", "tui", "clock-test", "")
    clock.observe(conn, removed_key, row)
    assert len(clock._started) == 2
    ticks[0] += 899
    clock.prune(conn)
    assert len(clock._started) == 1
    assert clock.observe(conn, active_key, row) == 899
    with kb.write_txn(conn):
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (args["task_id"],))
    assert notify.recover_orphaned_notify_claims(conn) == 0
    assert not clock._started


@pytest.mark.parametrize("column,value", [("inflight_start", 23), ("inflight_at", 1), ("last_event_id", 999),
                                          ("inflight_from", 987654), ("inflight_pid", 222)])
def test_rewind_fences_complete_snapshot(claim, column, value):
    conn, args, _, _, _, _ = claim
    row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (args["task_id"],)).fetchone()
    with kb.write_txn(conn):
        conn.execute(f"UPDATE kanban_notify_subs SET {column} = ? WHERE task_id = ?", (value, args["task_id"]))
        assert not notify._rewind_orphan(conn, notify._sub_key(**args, thread_id=None), row)
    assert notify.notify_claim_in_flight(conn, **args)
    assert clock._started


def test_recovery_reads_marker_after_entering_write_transaction(claim, monkeypatch):
    conn, args, ticks, _, _, _ = claim
    ticks[0] += 901
    original = kb.write_txn
    @contextlib.contextmanager
    def transaction(c):
        with original(c):
            # Another writer renewed the marker before recovery acquired its lock.
            c.execute("UPDATE kanban_notify_subs SET inflight_at = inflight_at + 1 WHERE task_id = ?",
                      (args["task_id"],))
            yield
    monkeypatch.setattr(kb, "write_txn", transaction)
    assert notify.recover_orphaned_notify_claims(conn) == 0
    assert notify.notify_claim_in_flight(conn, **args)


def test_dead_holder_recovers_immediately(claim, monkeypatch):
    conn, args, _, _, old, _ = claim
    monkeypatch.setattr(notify, "_holder_alive", lambda row: False)
    assert notify.recover_orphaned_notify_claims(conn) == 1
    assert notify._notify_cursor(conn, **args, thread_id=None) == old
    assert not clock._started


def test_foreign_claim_bootstraps_age_then_uses_monotonic_time(claim, monkeypatch):
    conn, args, ticks, _, _, _ = claim
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_notify_subs SET inflight_pid = 222, inflight_at = 1000 "
                     "WHERE task_id = ?", (args["task_id"],))
    monkeypatch.setattr(notify.time, "time", lambda: 1800)
    assert notify.recover_orphaned_notify_claims(conn) == 0
    monkeypatch.setattr(notify.time, "time", lambda: -1000000000)
    ticks[0] += 100
    assert notify.recover_orphaned_notify_claims(conn) == 1


def test_late_durable_ack_is_not_restricted_to_current_holder(claim, monkeypatch):
    conn, args, _, _, old, end = claim
    monkeypatch.setattr(notify, "_my_holder", lambda: (222, 33))
    assert notify.ack_notify_claim(conn, **args, upto=end, from_cursor=old)
    assert not notify.notify_claim_in_flight(conn, **args)
    assert not clock._started


def test_clock_does_not_execute_sql_under_lock(claim):
    conn, args, _, _, _, _ = claim
    class CheckedConnection:
        def execute(self, *a, **kw):
            assert not clock._lock.locked()
            return conn.execute(*a, **kw)
    checked = CheckedConnection()
    key = notify._sub_key(**args, thread_id=None)
    row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (args["task_id"],)).fetchone()
    clock.observe(checked, key, row)
    clock.prune(checked)
    clock.forget(checked, key)


def test_own_stuck_claim_is_replayed_by_next_claim_not_only_by_sweeper(claim):
    conn, args, ticks, _, old, _ = claim
    ticks[0] += notify.NOTIFY_CLAIM_STALE_S
    got_old, _new, events = notify.claim_unseen_events_for_sub(conn, **args, durable=True)
    assert got_old == old and events
    ticks[0] += 10
    assert notify.recover_orphaned_notify_claims(conn) == 0  # replay restarted the age


def test_own_marker_without_cache_starts_fresh_despite_old_wall_clock(claim, monkeypatch):
    conn, _args, _ticks, _, _, _ = claim
    with clock._lock:
        clock._started.clear()
    monkeypatch.setattr(notify.time, "time", lambda: 10 ** 10)
    assert notify.recover_orphaned_notify_claims(conn) == 0


@pytest.mark.parametrize("field", ["inflight_pid", "inflight_start", "inflight_at", "inflight_from"])
def test_observe_resets_only_when_identity_changes(claim, field):
    conn, _args, _, _, _, _ = claim
    base = {"inflight_pid": 1, "inflight_start": 2, "inflight_at": 3, "inflight_from": 4}
    key = ("k", "tui", "c", "")
    assert clock.observe(conn, key, base, now=0.0) == 0
    assert clock.observe(conn, key, base, now=50.0) == 50
    assert clock.observe(conn, key, {**base, field: 99}, now=60.0) == (50 + 10 if field == "inflight_from" else 0)


def test_observe_clamps_initial_age_and_backward_ticks(claim):
    conn = claim[0]
    key = ("k", "tui", "c", "")
    row = {"inflight_pid": 1, "inflight_start": 2, "inflight_at": 3}
    assert clock.observe(conn, key, row, now=100.0, initial_age=-50.0) == 0
    assert clock.observe(conn, key, row, now=110.0) == 10
    assert clock.observe(conn, key, row, now=90.0) == 0


def test_prune_is_scoped_to_database_skips_sql_when_empty_and_keeps_racing_observation(claim):
    conn, _args, _, _, _, _ = claim
    other = ("/elsewhere.db", "t", "tui", "c", "")
    with clock._lock:
        clock._started.clear()
        clock._started[other] = ((1, 2, 3), 5.0)
    clock.prune(conn)
    assert other in clock._started  # a different database's clock is not ours to evict

    class Boom:
        def execute(self, sql, *a):
            if sql.startswith("PRAGMA"):
                return conn.execute(sql, *a)
            raise AssertionError("empty cache must not query")
    clock.prune(Boom())

    database = clock._database(conn)
    racing = (database, "gone", "tui", "c", "")
    with clock._lock:
        clock._started[racing] = ((9, 9, 9), 1.0)
    renewed = ((8, 8, 8), 2.0)

    class Racing:
        def execute(self, sql, *a):
            if not sql.startswith("PRAGMA"):
                with clock._lock:
                    clock._started[racing] = renewed  # observed again after prune's snapshot
            return conn.execute(sql, *a)
    clock.prune(Racing())
    assert clock._started[racing] == renewed


def test_two_in_memory_connections_do_not_share_clock_state(claim):
    import sqlite3
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    key, row = ("k", "tui", "c", ""), {"inflight_pid": 1, "inflight_start": 2, "inflight_at": 3}
    assert clock.observe(a, key, row, now=0.0) == 0
    assert clock.observe(b, key, row, now=500.0) == 0


def test_expired_release_requires_own_marker_and_exact_bound(claim):
    conn, args, ticks, _, _, _ = claim
    S = notify.NOTIFY_CLAIM_STALE_S
    ticks[0] += S - 1
    assert not notify.release_expired_notify_claim(conn, **args)
    assert notify.notify_claim_in_flight(conn, **args)
    ticks[0] += 1
    assert notify.release_expired_notify_claim(conn, **args)
    assert not notify.notify_claim_in_flight(conn, **args)


def test_expired_release_ignores_old_foreign_marker(claim):
    conn, args, ticks, _event, _, _ = claim
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_notify_subs SET inflight_pid = 222 WHERE task_id = ?", (args["task_id"],))
    row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (args["task_id"],)).fetchone()
    clock.observe(conn, notify._sub_key(**args, thread_id=None), row, now=ticks[0],
                  initial_age=notify.NOTIFY_CLAIM_STALE_S + 60)
    assert not notify.release_expired_notify_claim(conn, **args)
    assert notify.notify_claim_in_flight(conn, **args)
