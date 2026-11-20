"""Blocked ancestor presentation on local and replica session task rows."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_session_activity_card_details as card_details
from hermes_cli.kanban_session_activity_card_details import _add_card_details


SCHEMA = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, containment_episode TEXT); "
    "CREATE TABLE task_links (parent_id TEXT, child_id TEXT); "
    "CREATE TABLE task_events (task_id TEXT, kind TEXT, uid TEXT, payload TEXT);")


@pytest.fixture
def board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


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


def seed_contained(conn):
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", [
        ("auth", "scheduled", None), ("leaf", "triage", "ep-1"),
        ("b", "todo", None), ("c", "todo", None), ("child", "todo", None)])
    conn.executemany("INSERT INTO task_links VALUES (?, ?)",
                     [("leaf", "b"), ("b", "c"), ("c", "child")])
    conn.execute("INSERT INTO task_events VALUES (?, ?, ?, ?)",
                 ("leaf", "contained", "ep-1", json.dumps({"policy": {"authority": {"task_id": "auth"}}})))


def assert_contained(row):
    assert row["blocked_upstream"] == {
        "id": "leaf", "status": "triage",
        "contained": {"episode": "ep-1", "authority": {"id": "auth", "status": "scheduled"}},
    }


def test_contained_triage_ancestor(board):
    seed_contained(board)
    found = {"child": {"id": "child", "status": "todo"}}
    _add_card_details(board, found)
    assert_contained(found["child"])


@pytest.fixture
def shadow(tmp_path, monkeypatch):
    from hermes_cli import kanban_sync_client
    path = tmp_path / "replica.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        seed_contained(conn)
    monkeypatch.setattr(kanban_sync_client, "shadow_path", lambda board: path)
    return path


def test_foreign_parent_resolved_through_replica(board, shadow, monkeypatch):
    from hermes_cli import kanban_db_sync
    monkeypatch.setattr(kanban_db_sync, "_db_key", lambda conn: str(shadow.parent / "local.db"))
    monkeypatch.setattr(kanban_db_sync, "board_slug_for_path", lambda key: "default")
    rows = details(board, [("child", "todo")], [("c", "child")], ["child"])
    assert_contained(rows["child"])


def test_remote_row_annotated_through_replica(shadow):
    rows = [{"id": "child", "status": "todo", "board": "default"}]
    card_details.annotate_remote_upstream(rows)
    assert_contained(rows[0])


def test_missing_replica_fails_soft(board, caplog, monkeypatch):
    monkeypatch.setattr(card_details, "_replica_for", lambda conn: None)
    with caplog.at_level("DEBUG"):
        rows = details(board, [("c", "todo")], [("b", "c")], ["c"])
    assert "blocked_upstream" not in rows["c"]
    assert "upstream" not in rows["c"]


def test_stale_replica_does_not_override_local_parent_status(tmp_path, monkeypatch):
    """Local details win: a done local parent is not rewritten from a lagging blocked replica."""
    from hermes_cli import kanban_db_sync, kanban_session_activity as ksa, kanban_sync_client
    from hermes_cli.kanban_session_outcome_tasks import append_remote_listed_cards

    local_path = tmp_path / "local.db"
    replica_path = tmp_path / "replica.db"
    local = sqlite3.connect(local_path)
    local.row_factory = sqlite3.Row
    local.executescript(SCHEMA)
    local.executemany("INSERT INTO tasks (id, status) VALUES (?, ?)", [("p", "done"), ("lc", "todo")])
    local.execute("INSERT INTO task_links VALUES (?, ?)", ("p", "lc"))
    local.commit()
    with sqlite3.connect(replica_path) as replica:
        replica.executescript(SCHEMA)
        replica.executemany("INSERT INTO tasks (id, status) VALUES (?, ?)",
                            [("p", "blocked"), ("lc", "todo")])
        replica.execute("INSERT INTO task_links VALUES (?, ?)", ("p", "lc"))
    monkeypatch.setattr(kanban_sync_client, "shadow_path", lambda board: replica_path)
    monkeypatch.setattr(kanban_db_sync, "_db_key", lambda conn: str(local_path))
    monkeypatch.setattr(kanban_db_sync, "board_slug_for_path", lambda key: "default")
    found = {"lc": {"id": "lc", "status": "todo", "board": "default"}}
    _add_card_details(local, found)
    assert "blocked_upstream" not in found["lc"]
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [{
        "key": "hub/default/default/remote", "id": "remote", "board": "default",
        "session": "s1", "status": "todo",
    }])
    out = [found["lc"]]
    append_remote_listed_cards(out, ["s1"], ksa.LISTED_STATUSES, False)
    local_row = next(row for row in out if row["id"] == "lc")
    assert "blocked_upstream" not in local_row
    local.close()
