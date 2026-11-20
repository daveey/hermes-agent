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


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(subject, "_NEXT_SCAN", {})


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
    _close(local, first[0], NOW)
    # Same tick, same membership: the closed incident still covers (no new card per tick).
    same = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    assert same == first
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    # Past the cooldown a still-overdue home gets a fresh repair.
    _fresh_at(replica, NOW + subject.HOME_REPLACE_AFTER_SECONDS + 1)
    later = subject.sweep(local, replica, "hub", now_ms=(NOW + subject.HOME_REPLACE_AFTER_SECONDS + 1) * 1000)
    assert later[1] == first[1] and later[0] != first[0]


def _fresh_at(replica, at):
    replica.execute("UPDATE kanban_sync_state SET pulled_at=?", (at,))


def _close(local, tid, at):
    local.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (at, tid))
    local.commit()


def test_closed_incident_does_not_respawn_every_tick(replica, local):
    card(replica, "one")
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    _close(local, first, NOW)
    for minutes in (1, 2, 60, 61, 6 * 60):
        _fresh_at(replica, NOW + minutes * 60)
        assert subject.sweep(local, replica, "hub", now_ms=(NOW + minutes * 60) * 1000) == [first]
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_second_closed_incident_covers_next_cycle(replica, local):
    card(replica, "one")
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    _close(local, first, NOW)
    at = NOW + subject.HOME_REPLACE_AFTER_SECONDS
    _fresh_at(replica, at - 1)
    assert subject.sweep(local, replica, "hub", now_ms=(at - 1) * 1000) == [first]
    _fresh_at(replica, at)
    [second] = subject.sweep(local, replica, "hub", now_ms=at * 1000)
    assert second != first
    _close(local, second, at)
    _fresh_at(replica, at + 60)
    assert subject.sweep(local, replica, "hub", now_ms=(at + 60) * 1000) == [second]
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.parametrize("payload", ['["one"]', 'not-json', '{}', '[[]]', '[1]'])
def test_membership_marker_is_validated(payload):
    body = "Home-overdue membership v1: " + payload + "\n- ghost (title), todo, 45 minutes overdue"
    assert subject._listed_ids(body) == ({"one"} if payload == '["one"]' else set())


def test_legacy_membership_listing_remains_readable():
    assert subject._listed_ids(
        "Home-owned transitions overdue\n- one (title), todo, 45 minutes overdue\n- tests (12 passed)"
    ) == {"one"}


def test_closed_incident_is_replaced_when_membership_changes(replica, local):
    card(replica, "one")
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    _close(local, first, NOW)
    card(replica, "two")
    _fresh_at(replica, NOW + 60)
    [second] = subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000)
    assert second != first


def test_closed_incident_is_replaced_when_membership_shrinks(replica, local):
    card(replica, "one")
    card(replica, "two")
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    _close(local, first, NOW)
    replica.execute("UPDATE tasks SET status='done' WHERE id='two'")
    _fresh_at(replica, NOW + 60)
    [second] = subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000)
    assert second != first
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_closed_incident_membership_ignores_checkpoint_bullets(replica, local):
    card(replica, "one")
    replica.execute(
        "INSERT INTO task_runs VALUES (1, 'one', 'continued', ?, ?)",
        ("status:\n- tests (12 passed)", NOW),
    )
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    task = kb.get_task(local, first)
    assert task is not None
    assert "\n- tests (12 passed)" in (task.body or "")
    _close(local, first, NOW)
    _fresh_at(replica, NOW + 60)
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000) == [first]
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("shape", ["checkpoint", "title"])
def test_closed_membership_is_not_derived_from_prose(replica, local, shape):
    card(replica, "one")
    if shape == "checkpoint":
        replica.execute(
            "INSERT INTO task_runs VALUES (1, 'one', 'continued', ?, ?)",
            ("quoted incident:\n- ghost (ghost title), todo, 45 minutes overdue (since x)", NOW),
        )
    else:
        replica.execute("UPDATE tasks SET title=? WHERE id='one'", ("multiline\ntitle",))
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    _close(local, first, NOW)
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000) == [first]
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_open_incident_observed_even_when_closed_sibling_matches(replica, local):
    card(replica, "one")
    [open_id] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    body, key = local.execute("SELECT body, idempotency_key FROM tasks WHERE id=?", (open_id,)).fetchone()
    local.execute(
        "INSERT INTO tasks (id, title, body, status, created_by, created_at, completed_at, "
        "workspace_kind, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
        ("closed-sib", "closed", body, "done", kps.DETECTOR, NOW, NOW + 1, "scratch", key + "z"),
    )
    local.commit()
    _fresh_at(replica, NOW + 60)
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000) == [open_id]
    assert len(kb.list_comments(local, open_id)) == 1


def test_archived_without_completed_at_still_covers(replica, local):
    card(replica, "one")
    [first] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    local.execute(
        "UPDATE tasks SET status='archived', completed_at=NULL, created_at=? WHERE id=?",
        (NOW - subject.HOME_REPLACE_AFTER_SECONDS - 10, first),
    )
    local.commit()
    _fresh_at(replica, NOW)
    assert subject.sweep(local, replica, "hub", now_ms=NOW * 1000) == [first]
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


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


@pytest.mark.parametrize("age", [600, -5])
def test_replica_freshness_includes_exact_boundaries(replica, local, age):
    card(replica)
    replica.execute("UPDATE kanban_sync_state SET pulled_at=?", (NOW - age,))
    [tid] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    task = kb.get_task(local, tid)
    assert task is not None
    assert task.created_by == kps.DETECTOR


def test_registry_requires_fresh_evidence(replica, local, monkeypatch):
    card(replica)
    calls = []

    def registry(*, require_fresh=False):
        calls.append(require_fresh)
        # A stale cached registry must not authorize the home-silence alarm.
        return None if require_fresh else {"machines": {"worker-2": {"hold": True}}}

    monkeypatch.setattr(wn, "registry", registry)
    assert subject.sweep(local, replica, "hub", now_ms=NOW * 1000) == []
    assert calls == [True]
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_held_home_heartbeats_do_not_churn_incident_comments(replica, local):
    card(replica)
    [tid] = subject.sweep(local, replica, "hub", now_ms=NOW * 1000)
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 60) * 1000) == [tid]
    comments = kb.list_comments(local, tid)
    assert len(comments) == 1
    replica.execute("INSERT INTO task_events(kind, created_at, origin_gateway) "
                    "VALUES ('heartbeat', ?, 'worker-2')", (NOW + 61,))
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 120) * 1000) == [tid]
    assert [c.body for c in kb.list_comments(local, tid)] == [c.body for c in comments]
    replica.execute("DELETE FROM task_events WHERE kind='heartbeat'")
    assert subject.sweep(local, replica, "hub", now_ms=(NOW + 180) * 1000) == [tid]
    assert [c.body for c in kb.list_comments(local, tid)] == [c.body for c in comments]


@pytest.mark.parametrize("park", ["missing", "spent-woken", "spent-claimed"])
def test_missing_or_spent_park_does_not_reuse_old_wake(replica, local, monkeypatch, park):
    from hermes_cli import kanban_continuation as continuation

    monkeypatch.setattr(continuation.time, "time", lambda: NOW)
    card(replica)
    if park == "missing":
        replica.execute("DELETE FROM task_events WHERE task_id='one'")
    else:
        kind = continuation.CONTINUATION_WOKEN_EVENT if park == "spent-woken" else "claimed"
        replica.execute("INSERT INTO task_events(task_id, kind, created_at, origin_gateway) "
                        "VALUES ('one', ?, ?, 'worker-2')", (kind, NOW - 10))
    assert subject.sweep(local, replica, "hub", now_ms=NOW * 1000) == []
    assert local.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.fixture
def observer_replica(replica, local, monkeypatch):
    from hermes_cli import kanban_db_sync as sync

    card(replica)
    replica.commit()
    opened = []

    def open_replica(conn):
        assert conn is local
        copy = sqlite3.connect(":memory:")
        copy.row_factory = sqlite3.Row
        replica.backup(copy)
        opened.append(copy)
        return copy

    monkeypatch.setattr(sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(wn, "_own", lambda conn: "hub")
    monkeypatch.setattr(wn, "open_replica", open_replica)
    return opened


@pytest.mark.parametrize("pickup,draining", [(None, False), (None, True), ("pinned", True)])
def test_dispatcher_observes_homes_before_pickup_admission(
        local, observer_replica, monkeypatch, pickup, draining):
    from hermes_cli import kanban_gateway_drain as drain

    monkeypatch.setattr(wn, "pickup_mode", lambda: pickup)
    monkeypatch.setattr(drain, "dispatch_paused", lambda: draining)
    [tid] = kps.sweep(local, now=NOW)
    task = kb.get_task(local, tid)
    assert task is not None
    assert "Home transitions overdue on worker-2" in task.title
    assert task.created_by == kps.DETECTOR
    assert len(observer_replica) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observer_replica[0].execute("SELECT 1")


@pytest.mark.parametrize("failure", ["open", "scan"])
def test_observer_failure_does_not_suppress_ready_alarm(
        local, observer_replica, monkeypatch, failure):
    from hermes_cli import kanban_gateway_drain as drain
    from hermes_cli import kanban_pickup_stall_held as held

    monkeypatch.setattr(wn, "pickup_mode", lambda: "pinned")
    monkeypatch.setattr(drain, "dispatch_paused", lambda: False)
    monkeypatch.setattr(held, "sweep", lambda *a, **kw: [])
    monkeypatch.setattr(kps, "resolve_sweep", lambda *a, **kw: [])
    ready = {"id": "ready-card", "title": "ready title", "tenant": None,
             "since_ms": (NOW - 1200) * 1000,
             "skip": {"reason": "profile_ineligible", "detail": "missing profile",
                      "first_at": (NOW - 1200) * 1000, "last_at": NOW * 1000, "passes": 1}}
    monkeypatch.setattr(kps, "stalled", lambda *a, **kw: [ready])

    def fail(*a, **kw):
        raise RuntimeError("observer unavailable")

    if failure == "open":
        original_open = wn.open_replica
        calls = []

        def fail_first(conn):
            calls.append(conn)
            if len(calls) == 1:
                return fail()
            return original_open(conn)

        monkeypatch.setattr(wn, "open_replica", fail_first)
    else:
        monkeypatch.setattr(subject, "periodic_sweep", fail)

    [tid] = kps.sweep(local, now=NOW)
    task = kb.get_task(local, tid)
    assert task is not None
    assert "ready-card" in task.title
    assert "profile_ineligible" in (task.body or "")
    assert task.created_by == kps.DETECTOR
    assert len(observer_replica) == (1 if failure == "open" else 2)
    for conn in observer_replica:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
