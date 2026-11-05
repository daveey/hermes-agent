"""Malformed peer evidence must not poison the dispatcher or lifecycle writer."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_wake_episode as base

kanban_home = base.kanban_home
parked = base.parked


@pytest.mark.parametrize("raw", [
    'broken "_wake_episode_v1"',
    '{"_wake_episode_v1": {"cmd": "exit 1"}}',
    '{"_wake_episode_v1": false}',
    '{"meta": {"_wake_episode_v1": null}}',
    '["_wake_episode_v1"]',
])
def test_bad_snapshot_does_not_poison_probe_or_blocked_event(parked, raw):
    conn, tid, clock = parked
    base.result(parked, 0)
    with kb.write_txn(conn):
        cur = conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                           (tid, "foreign", raw, clock[0]))
        event_id = cur.lastrowid
    assert base.result(parked, 1) is None
    assert wc.get_state(conn, tid)["episode_checks"] == 2
    assert conn.execute("SELECT event_id FROM task_wake_episode_seen WHERE task_id = ?", (tid,)).fetchone()[0] == event_id
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "blocked")


@pytest.mark.parametrize("key,value", [
    ("cmd", None), ("cmd", ""), ("cmd", 123), ("first_at", -1), ("first_at", True),
    ("checks", 0), ("checks", "5"), ("checks", False), ("last_stale_at", "later"),
    ("last_stale_at", -1),
])
def test_snapshot_fields_are_validated(parked, key, value):
    conn, tid, clock = parked
    base.result(parked, 0)
    data = {"cmd": "exit 1", "first_at": clock[0], "checks": 1, "last_stale_at": None}
    data[key] = value
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                     (tid, "foreign", json.dumps({"_wake_episode_v1": data}), clock[0]))
    assert base.result(parked, 48) == base.STALE


def test_older_home_snapshot_cannot_reduce_executor_count(parked):
    conn, tid, clock = parked
    base.result(parked, 0)
    for hour in (1, 2, 3):
        base.result(parked, hour)
    data = {"cmd": "exit 1", "first_at": clock[0], "checks": 1, "last_stale_at": None}
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                     (tid, "continuation_woken", json.dumps({"_wake_episode_v1": data}), clock[0]))
    assert base.result(parked, 4) is None
    assert wc.get_state(conn, tid)["episode_checks"] == 5
