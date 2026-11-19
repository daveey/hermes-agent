"""Blocked ancestor presentation on local session task rows."""
import sqlite3

import pytest

from hermes_cli.kanban_session_activity_card_details import _add_card_details


@pytest.fixture
def board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT); "
                       "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);")
    yield conn
    conn.close()


def details(conn, statuses, links, listed):
    conn.executemany("INSERT INTO tasks VALUES (?, ?)", statuses)
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
