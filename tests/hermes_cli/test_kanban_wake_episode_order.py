"""Delayed starts/resets must respect reset causality, independent of clocks."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_episode as episode
from hermes_cli import kanban_wake_episode_barriers as barriers
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_wake_episode as base
from tests.hermes_cli import test_kanban_wake_episode_carry as carry

kanban_home = base.kanban_home
parked = base.parked


def block_and_repark(conn, tid, clock):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "blocked")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    base.repark((conn, tid, clock))


def test_delayed_old_start_cannot_resurrect_after_unobserved_reset(parked):
    conn, tid, clock = parked
    other = carry.replica(conn)
    try:
        base.result(parked, 0)
        block_and_repark(other, tid, clock)
        carry.import_events(conn, other, tid)
        assert base.result((other, tid, clock), 48) is None
        assert wc.get_state(other, tid)["episode_first_at"] == clock[0] + 48 * base.HOUR
    finally:
        other.close()


def test_duplicate_old_reset_does_not_erase_new_episode(parked):
    conn, tid, clock = parked
    base.result(parked, 0)
    block_and_repark(conn, tid, clock)
    base.result(parked, 24)
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'", (tid,)).fetchone()[0]
    conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                 (tid, "foreign", payload, clock[0] - 9999))
    assert base.result(parked, 72) == base.STALE
    assert base.helpers._events(conn, tid, base.STALE)[0].payload["checks"] == 2


def test_concurrent_resets_join_without_accepting_either_old_episode(parked):
    conn, tid, clock = parked
    other = carry.replica(conn)
    try:
        block_and_repark(conn, tid, clock)
        block_and_repark(other, tid, clock)
        base.result(parked, 10)
        base.result((other, tid, clock), 20)
        carry.import_events(conn, other, tid)
        episode.reconcile(other, tid)
        assert wc.get_state(other, tid)["episode_first_at"] is None
        assert len(barriers.current(other, tid)) == 2
        base.result((other, tid, clock), 30)
        carry.import_events(other, conn, tid)
        episode.reconcile(conn, tid)
        assert wc.get_state(conn, tid)["episode_first_at"] == clock[0] + 30 * base.HOUR
    finally:
        other.close()


def test_changed_probe_before_start_arrives_invalidates_old_episode(parked):
    conn, tid, clock = parked
    other = carry.replica(conn)
    try:
        base.result(parked, 0)
        with kb.write_txn(other):
            wc.set_check(other, tid, "exit 7", 120)
            kb._append_event(other, tid, "scheduled", {"wake_check": "exit 7"})
            wc.set_check(other, tid, "exit 1", 120)
            kb._append_event(other, tid, "scheduled", {"wake_check": "exit 1"})
        carry.import_events(conn, other, tid)
        assert base.result((other, tid, clock), 48) is None
        assert wc.get_state(other, tid)["episode_first_at"] == clock[0] + 48 * base.HOUR
    finally:
        other.close()


@pytest.mark.parametrize("value", [None, [], {"x": False}, {"x": -1}, {"": 1}, {"x": "1"}])
def test_invalid_barrier_does_not_erase_episode(parked, value):
    conn, tid, clock = parked
    base.result(parked, 0)
    conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                 (tid, "foreign", json.dumps({"_wake_episode_v1": None, barriers.KEY: value}), clock[0]))
    assert base.result(parked, 48) == base.STALE
