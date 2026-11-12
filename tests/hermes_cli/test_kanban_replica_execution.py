"""A running card on another gateway, read from its synced Board replica, is dated by its executor's own
heartbeat events for the CURRENT run, never by the replica's pull clock; anything less stays explicit unknown."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_execution_observations import add_cards, count_snapshot

ME, OWNER = "macbook", "hub"
NOW = 10_000.0
RUN_UID, OLD_UID = "01run-current", "01run-old"


def _replica(path, *, pulled=NOW, status="running", run_status="running", ended=None, beats=((NOW - 30, "e2"),),
             old_beats=((NOW - 5, "e-old"),)):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT, "
        "current_run_id INTEGER);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, uid TEXT, status TEXT, ended_at INTEGER);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, "
        "created_at INTEGER, uid TEXT);"
        "CREATE INDEX idx_events_run ON task_events(run_id, id);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
        "chat_session TEXT, kind TEXT, released INTEGER, created_at INTEGER);"
        "CREATE TABLE kanban_sync_state (pulled_at REAL);")
    conn.execute("INSERT INTO tasks VALUES ('t_run', 'work', ?, 'worker', 1, 1, 0, NULL, NULL, ?, 2)", (status, OWNER))
    conn.execute("INSERT INTO task_runs VALUES (1, 't_run', ?, 'released', 50)", (OLD_UID,))
    conn.execute("INSERT INTO task_runs VALUES (2, 't_run', ?, ?, ?)", (RUN_UID, run_status, ended))
    for at, uid in old_beats:  # a previous attempt's late-arriving heartbeat: never this run's clock
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, created_at, uid) VALUES ('t_run', 1, 'heartbeat', ?, ?)",
                     (at, uid))
    for at, uid in beats:
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, created_at, uid) VALUES ('t_run', 2, 'heartbeat', ?, ?)",
                     (at, uid))
    # An ordinary comment of the run is activity, not a liveness clock.
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, created_at, uid) VALUES ('t_run', 2, 'commented', ?, 'c1')",
                 (NOW - 1,))
    conn.execute("INSERT INTO task_chat_links VALUES (?, 't_run', ?, 'default', 's1', 'created', 0, 1)",
                 (links.link_uid("t_run", ME, "default", "s1", "created"), ME))
    conn.execute("INSERT INTO kanban_sync_state VALUES (?)", (pulled,))
    conn.commit()
    conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    from hermes_cli import profiles
    links._reset_for_tests()
    named = [{"id": "t_run", "board": "default", "gateway": OWNER, "profile": "work", "session": "s1",
              "released": False}]
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: list(named))
    yield tmp_path / "default.db", named
    links._reset_for_tests()


def _rows(path, now=NOW):
    return links.replica_cards(["s1"], ME, replicas=[("default", path)], now=now)


def _observe(path, *, now=NOW, read_at=NOW):
    rows = [{**card, "session_id": card["session"]} for card in _rows(path, now=read_at)]
    return add_cards(rows, now=now)[0]["execution_observations"][0]


def test_current_run_heartbeat_dates_a_remote_running_card(world):
    path, _ = world
    _replica(path, beats=((NOW - 90, "e1"), (NOW - 30, "e2"), (NOW - 60, "e3")))  # arrival order != clock order
    obs = _observe(path)
    assert obs["state"] == "running" and obs["freshness"] == "current"
    assert obs["identity"] == {"gateway": OWNER, "profile": "work", "board": "default", "kind": "card",
                               "work_id": "t_run", "run_id": RUN_UID}
    assert obs["observed_at"] == NOW - 30 and obs["valid_until"] == NOW + 150
    assert obs["source_revision"] == "event:e2" and obs["evidence_refs"] == ["event:e2"]
    assert obs["last_progress"] is None  # a heartbeat is liveness, never progress


def test_time_alone_expires_the_cached_running_record(world):
    path, _ = world
    _replica(path)
    obs = _observe(path, now=NOW + 150, read_at=NOW)  # same read, later render: no refetch renews it
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "source_expired"
    assert obs["last_known_state"] == "running"


def test_pull_clock_never_rejuvenates_an_old_heartbeat(world):
    path, _ = world
    _replica(path, pulled=NOW, beats=((NOW - 400, "e1"),), old_beats=())
    obs = _observe(path)
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "source_expired"


@pytest.mark.parametrize("change", ["old_run_only", "run_ended", "run_released", "parked", "unnamed_profile",
                                    "future_clock"])
def test_missing_or_foreign_run_evidence_stays_unknown(world, change):
    path, named = world
    options = {"old_run_only": dict(beats=()), "run_ended": dict(ended=NOW - 10),
               "run_released": dict(run_status="released"), "parked": dict(status="scheduled"),
               "future_clock": dict(beats=((NOW + 60, "e9"),))}.get(change, {})
    if change == "unnamed_profile":
        named.clear()  # the replica row names no owner profile; ``default`` is only a label
    _replica(path, **options)
    obs = _observe(path)
    assert obs["state"] == "unknown"
    if change == "future_clock":
        assert obs["unknown_reason"] == "clock_inconsistent"
    else:
        assert obs["identity"].get("run_id") is None
        assert obs["source"] == "kanban.card"


def test_stale_replica_keeps_identity_but_is_unknown(world):
    path, _ = world
    _replica(path, pulled=NOW - 10_000)
    obs = _observe(path)
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "source_unavailable"
    assert obs["identity"]["run_id"] == RUN_UID and obs["last_known_state"] == "running"


def test_counts_and_task_list_project_the_same_record(world):
    path, _ = world
    _replica(path)
    (card,) = _rows(path)
    evidence = {}
    links_mod = __import__("hermes_cli.kanban_replica_execution", fromlist=["collect"])
    links_mod.collect(card, evidence)
    row = {"kanban_active": 1}
    count_snapshot(row, [(card["key"], "active", False)], now=NOW, evidence=evidence["s1"])
    listed = _observe(path)
    assert row["execution_observations"] == [listed]
    assert "_replica_run" not in add_cards([{**card, "session_id": "s1"}], now=NOW)[0]


def test_older_replica_schema_reads_no_run_columns(world, tmp_path):
    path, _ = world
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
        "chat_session TEXT, kind TEXT, released INTEGER, created_at INTEGER);"
        "CREATE TABLE kanban_sync_state (pulled_at REAL);")
    conn.execute("INSERT INTO tasks VALUES ('t_run', 'work', 'running', 'worker', 1, 1, 0, NULL, NULL, ?)", (OWNER,))
    conn.execute("INSERT INTO task_chat_links VALUES (?, 't_run', ?, 'default', 's1', 'created', 0, 1)",
                 (links.link_uid("t_run", ME, "default", "s1", "created"), ME))
    conn.execute("INSERT INTO kanban_sync_state VALUES (?)", (NOW,))
    conn.commit()
    conn.close()
    (card,) = _rows(path)
    assert "_replica_run" not in card
    assert _observe(path)["unknown_reason"] == "missing_qualified_identity"
