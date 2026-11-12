"""Event-kind lookups must scale with that kind, not the full heartbeat history."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


INDEX = "idx_events_kind_task_id"
CREATORS = ("SELECT task_id FROM task_events WHERE kind='created' "
            "AND json_extract(payload,'$.creator_task_id')=?")
LATEST_MOVE = "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='moved'"


@pytest.fixture
def history():
    conn = sqlite3.connect(":memory:")
    conn.executescript(kb.SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
        ((f"t_{i % 5000}", "created" if i < 5000 else "moved" if i < 5576 else "heartbeat",
          f'{{"creator_task_id":"t_{i % 100}"}}', i) for i in range(320000)),
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _query_with_budget(conn, sql, args, budget):
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return int(steps > budget)

    conn.set_progress_handler(progress, 100)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.set_progress_handler(None, 0)


def test_creator_search_does_not_scan_heartbeat_history(history):
    # Exact production query from kanban-honest-done.related_task_ids.
    rows = _query_with_budget(history, CREATORS, ("t_0",), 80000)
    assert {row[0] for row in rows} == {f"t_{i}" for i in range(0, 5000, 100)}
    assert any(INDEX in row[3] for row in history.execute("EXPLAIN QUERY PLAN " + CREATORS, ("t_0",)))


def test_latest_move_uses_kind_and_id_order(history):
    # A long history of later non-move events must not obscure the latest move.
    assert _query_with_budget(history, LATEST_MOVE, ("t_0",), 100) == [(5001,)]
    assert _query_with_budget(history, LATEST_MOVE, ("t_4999",), 100) == [(None,)]
    assert any(INDEX in row[3] for row in history.execute("EXPLAIN QUERY PLAN " + LATEST_MOVE, ("t_0",)))


def test_existing_integer_schema_gets_index_on_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(kb.SCHEMA_SQL)
        conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('t_0','created','{}',1)")
    for _ in range(2):
        kb._INITIALIZED_PATHS.discard(str(path.resolve()))
        with kbc.connect(path) as conn:
            assert [row[2] for row in conn.execute(f"PRAGMA index_info({INDEX})")] == ["kind", "task_id", "id"]
            assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1
