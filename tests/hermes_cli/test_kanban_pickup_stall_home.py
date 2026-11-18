"""Cross-home scheduled/todo alarms, including real incident filing. [daveey]"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_pickup_stall_home as subject
from hermes_cli import kanban_worker_node as wn

NOW = 2_000_000_000


@pytest.fixture
def replica():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, created_on TEXT,
                            tenant TEXT, completed_at INTEGER, block_kind TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
                                  payload TEXT, created_at INTEGER, origin_gateway TEXT, run_id INTEGER);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT,
                                summary TEXT, ended_at INTEGER);
    """)
    conn.execute("CREATE TABLE kanban_sync_state (pulled_at INTEGER)")
    conn.execute("INSERT INTO kanban_sync_state VALUES (?)", (NOW,))
    yield conn
    conn.close()


def card(replica, tid="one", *, minutes=31, home="worker-2", status="scheduled", wake=None):
    replica.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, NULL, NULL, NULL)", (tid, tid + " title", status, home))
    replica.execute("INSERT INTO task_events(task_id, kind, payload, created_at, origin_gateway) "
                    "VALUES (?, 'scheduled', ?, ?, ?)",
                    (tid, json.dumps(wake if wake is not None else {"wake_at": NOW - minutes * 60}),
                     NOW - 86400, home))
    return tid


def parents(replica, tid="one", *, minutes=31, status="done"):
    replica.execute("INSERT INTO tasks VALUES ('parent', 'parent', ?, 'hub', NULL, ?, NULL)",
                    (status, NOW - minutes * 60))
    replica.execute("INSERT INTO task_links VALUES ('parent', ?)", (tid,))


@pytest.mark.parametrize("minutes,want", [(29, False), (30, False), (31, True)])
def test_threshold(replica, minutes, want):
    card(replica, minutes=minutes)
    assert bool(subject.overdue(replica, {"worker-2": {"hold": True}}, now=NOW)) is want


@pytest.mark.parametrize("status", ["scheduled", "todo"])
def test_recent_home_event_suppresses_unheld_home(replica, status):
    card(replica, status=status)
    if status == "todo":
        parents(replica)
    replica.execute("INSERT INTO task_events(kind, created_at, origin_gateway) VALUES ('heartbeat', ?, 'worker-2')",
                    (NOW - 29 * 60,))
    assert not subject.overdue(replica, {"worker-2": {"hold": False}}, now=NOW)
    assert subject.overdue(replica, {"worker-2": {"hold": True}}, now=NOW)
    assert subject.overdue(replica, {"worker-2": {"state": "revoked"}}, now=NOW)


def test_silent_home_and_missing_registry_entry(replica):
    card(replica)
    assert subject.overdue(replica, {}, now=NOW)


@pytest.mark.parametrize("minutes,status,want", [(31, "done", True), (29, "done", False), (31, "todo", False)])
def test_parent_completion_clock(replica, minutes, status, want):
    card(replica, status="todo")
    parents(replica, minutes=minutes, status=status)
    assert bool(subject.overdue(replica, {"worker-2": {"hold": True}}, now=NOW)) is want


def test_no_parents_or_missing_parent_is_not_proven_ready(replica):
    card(replica, status="todo")
    assert not subject.overdue(replica, {}, now=NOW)
    replica.execute("INSERT INTO task_links VALUES ('missing', 'one')")
    assert not subject.overdue(replica, {}, now=NOW)


def test_latest_parent_clock_and_terminal_exclusion(replica):
    card(replica, status="todo")
    parents(replica)
    replica.execute("INSERT INTO tasks VALUES ('new', 'new', 'done', 'hub', NULL, ?, NULL)", (NOW - 29 * 60,))
    replica.execute("INSERT INTO task_links VALUES ('new', 'one')")
    assert not subject.overdue(replica, {}, now=NOW)
    replica.execute("UPDATE tasks SET status='done' WHERE id='one'")
    assert not subject.overdue(replica, {}, now=NOW)


def test_checkpoint_custody_is_conservative(replica):
    card(replica)
    assert subject.checkpoint_evidence(replica, "one").startswith("unknown")
    replica.execute("INSERT INTO task_runs VALUES (1, 'one', 'continued', 'unpushed worktree ~/code/fix', ?)", (NOW,))
    assert "possible machine-only WIP" in subject.checkpoint_evidence(replica, "one")
    replica.execute("INSERT INTO task_runs VALUES (2, 'one', 'continued', 'Published PR123; no further edits', ?)", (NOW + 1,))
    assert subject.checkpoint_evidence(replica, "one").startswith("not named")


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("toolsets: [all]\n", encoding="utf-8")
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "alarms.db"
    kb.init_db(path)
    from hermes_cli.kanban_db_connect import connect
    conn = connect(path)
    conn.execute("ALTER TABLE tasks ADD COLUMN runs_on TEXT")
    monkeypatch.setattr(wn, "registry", lambda **kw: {"machines": {"worker-2": {"hold": True}}})
    yield conn
    conn.close()


def test_one_incident_per_home_and_second_pass_comments(replica, local):
    card(replica, "one")
    card(replica, "two")
    [tid] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    task = kb.get_task(local, tid)
    assert task.created_by == kps.DETECTOR
    assert "one title" in task.body and "two title" in task.body
    assert "31 minutes overdue" in task.body
    assert "pool:worker" in local.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert task.idempotency_key.startswith("pickup-stall:hub:home-overdue-worker-2:")
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000) == [tid]
    assert len(kb.list_comments(local, tid)) == 1
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 120) * 1000) == [tid]
    assert len(kb.list_comments(local, tid)) == 1  # elapsed minutes alone never churn
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_observer_is_hub_only(replica, local):
    card(replica)
    assert subject.sweep(local, replica, "worker-1", now_ms=NOW * 1000) == []
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("age", [601, -6, None])
def test_stale_or_unknown_replica_never_vouches_for_silence(replica, local, age):
    card(replica)
    replica.execute("UPDATE kanban_sync_state SET pulled_at=?", (None if age is None else NOW - age,))
    assert subject.sweep(local, replica, "hub", now_ms=NOW * 1000) == []


def test_missing_registry_defers(replica, local, monkeypatch):
    card(replica)
    monkeypatch.setattr(wn, "registry", lambda **kw: None)
    assert subject.sweep(local, replica, "hub", now_ms=NOW * 1000) == []


def test_periodic_scan_is_bounded_and_restarts_for_replaced_file(replica, local, monkeypatch):
    from hermes_cli import kanban_sweep_gate
    subject._NEXT_SCAN.clear()
    clock = [100.0]
    identity = [("path", 1, 2)]
    calls = []
    monkeypatch.setattr(subject.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(kanban_sweep_gate, "file_identity", lambda conn: identity[0])
    monkeypatch.setattr(subject, "sweep", lambda *a, **kw: calls.append(kw) or ["alarm"])
    assert subject.periodic_sweep(local, replica, "hub", now_ms=NOW * 1000) == ["alarm"]
    clock[0] = 159
    assert subject.periodic_sweep(local, replica, "hub", now_ms=NOW * 1000) == []
    clock[0] = 160
    assert subject.periodic_sweep(local, replica, "hub", now_ms=NOW * 1000) == ["alarm"]
    identity[0] = ("path", 1, 3)
    assert subject.periodic_sweep(local, replica, "hub", now_ms=NOW * 1000) == ["alarm"]
    assert len(calls) == 3
    assert subject.periodic_sweep(local, replica, "worker-1", now_ms=NOW * 1000) == []


def test_separate_homes_and_closed_incidents(replica, local):
    card(replica, "one")
    card(replica, "two", home="worker-7")
    first = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    assert len(first) == 2
    local.execute("UPDATE tasks SET status='done' WHERE id=?", (first[0],))
    local.commit()
    again = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    assert first[1] == again[1] and first[0] != again[0]


def test_legacy_backstop_and_current_park(replica):
    from hermes_cli.kanban_timed_park import implied_wake
    card(replica, wake={})
    expected = implied_wake({}, NOW - 86400)
    row = replica.execute("SELECT * FROM tasks WHERE id='one'").fetchone()
    assert subject._scheduled_since(replica, row) == expected
    replica.execute("INSERT INTO task_events(task_id,kind,payload,created_at,origin_gateway) "
                    "VALUES ('one','scheduled',?,?,'worker-2')",
                    (json.dumps({"wake_at": NOW + 3600}), NOW - 10))
    assert not subject.overdue(replica, {}, now=NOW)
