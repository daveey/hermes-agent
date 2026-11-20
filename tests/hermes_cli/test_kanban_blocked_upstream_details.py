"""Blocked ancestor presentation on local session task rows."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_session_activity_card_details as card_details
from hermes_cli.kanban_session_activity_card_details import _add_card_details


@pytest.fixture
def board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, containment_episode TEXT); "
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT); "
        "CREATE TABLE task_events (task_id TEXT, kind TEXT, uid TEXT, payload TEXT);")
    yield conn
    conn.close()


def replica_board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, containment_episode TEXT); "
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT); "
        "CREATE TABLE task_events (task_id TEXT, kind TEXT, uid TEXT, payload TEXT);")
    return conn


def details(conn, statuses, links, listed):
    conn.executemany("INSERT INTO tasks (id, status) VALUES (?, ?)", statuses)
    conn.executemany("INSERT INTO task_links VALUES (?, ?)", links)
    found = {key: {"id": key, "status": dict(statuses)[key]} for key in listed}
    _add_card_details(conn, found)
    return found


@pytest.mark.parametrize("status", ["blocked", "triage", "running"])
def test_chain_and_cycle(board, status):
    rows = details(board, [("a", status), ("b", "todo"), ("c", "todo")],
                   [("a", "b"), ("b", "c"), ("c", "a")], ["b", "c"])
    for row in rows.values():
        if status == "running":
            assert "blocked_upstream" not in row
        else:
            assert row["blocked_upstream"] == {"id": "a", "status": status}


def test_nearest_ancestor(board):
    rows = details(board, [("a", "blocked"), ("b", "triage"), ("c", "todo")],
                   [("a", "b"), ("b", "c")], ["c"])
    assert rows["c"]["blocked_upstream"] == {"id": "b", "status": "triage"}


@pytest.mark.parametrize("status", ["done", "archived", "blocked", "triage"])
def test_excluded_card(board, status):
    rows = details(board, [("a", "blocked"), ("b", status)], [("a", "b")], ["b"])
    assert "blocked_upstream" not in rows["b"]


def test_depth_cap(board):
    statuses = [(str(i), "blocked" if i == 0 else "todo") for i in range(22)]
    rows = details(board, statuses, [(str(i), str(i + 1)) for i in range(21)], ["20", "21"])
    assert rows["20"]["blocked_upstream"]["id"] == "0"
    assert "blocked_upstream" not in rows["21"]


def test_missing_links_fails_soft(board, caplog):
    board.execute("DROP TABLE task_links")
    found = {"b": {"id": "b", "status": "todo"}}
    with caplog.at_level("DEBUG"):
        _add_card_details(board, found)
    assert "blocked_upstream" not in found["b"]
    assert "blocked upstream" in caplog.text.lower()


def test_upstream_when_no_blocked_ancestor(board):
    rows = details(board, [("a", "running"), ("b", "todo")], [("a", "b")], ["b"])
    assert "blocked_upstream" not in rows["b"]
    assert rows["b"]["upstream"] == {"id": "a", "status": "running"}


def test_contained_triage_ancestor(board):
    board.executemany("INSERT INTO tasks VALUES (?, ?, ?)",
                      [("auth", "done", None), ("leaf", "triage", "ep-1"), ("child", "todo", None)])
    board.execute("INSERT INTO task_links VALUES (?, ?)", ("leaf", "child"))
    board.execute("INSERT INTO task_events VALUES (?, ?, ?, ?)",
                  ("leaf", "contained", "ep-1", json.dumps({"policy": {"authority": {"task_id": "auth"}}})))
    found = {"child": {"id": "child", "status": "todo"}}
    _add_card_details(board, found)
    assert found["child"]["blocked_upstream"] == {
        "id": "leaf", "status": "triage",
        "contained": {"episode": "ep-1", "authority": {"id": "auth", "status": "done"}},
    }


def test_foreign_parent_resolved_through_replica(board):
    replica = replica_board()
    replica.executemany("INSERT INTO tasks VALUES (?, ?, ?)",
                        [("a", "blocked", None), ("b", "todo", None), ("c", "todo", None)])
    replica.executemany("INSERT INTO task_links VALUES (?, ?)", [("a", "b"), ("b", "c")])
    card_details._replica_for = lambda conn: replica
    try:
        rows = details(board, [("c", "todo")], [("b", "c")], ["c"])
        assert rows["c"]["blocked_upstream"] == {"id": "a", "status": "blocked"}
    finally:
        card_details._replica_for = lambda conn: None
        replica.close()


def test_remote_row_annotated_through_replica(board):
    replica = replica_board()
    replica.executemany("INSERT INTO tasks VALUES (?, ?, ?)",
                        [("a", "triage", "ep-1"), ("c", "todo", None)])
    replica.execute("INSERT INTO task_links VALUES (?, ?)", ("a", "c"))
    replica.execute("INSERT INTO task_events VALUES (?, ?, ?, ?)",
                    ("a", "contained", "ep-1", json.dumps({"policy": {"authority": {"task_id": "auth"}}})))
    replica.execute("INSERT INTO tasks VALUES (?, ?, ?)", ("auth", "scheduled", None))
    card_details._replica_for = lambda conn: replica
    try:
        found = {"c": {"id": "c", "status": "todo"}}
        _add_card_details(board, found)
        assert found["c"]["blocked_upstream"] == {
            "id": "a", "status": "triage",
            "contained": {"episode": "ep-1", "authority": {"id": "auth", "status": "scheduled"}},
        }
    finally:
        card_details._replica_for = lambda conn: None
        replica.close()


def test_missing_replica_fails_soft(board, caplog):
    card_details._replica_for = lambda conn: None
    try:
        with caplog.at_level("DEBUG"):
            rows = details(board, [("c", "todo")], [("b", "c")], ["c"])
        assert "blocked_upstream" not in rows["c"]
        assert "upstream" not in rows["c"]
    finally:
        card_details._replica_for = lambda conn: None
