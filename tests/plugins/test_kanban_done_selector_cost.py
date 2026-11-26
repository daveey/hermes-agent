"""The done-window prepass must not traverse dispatcher dependency graphs."""
import sqlite3
from unittest import mock

import pytest

from plugins.kanban.dashboard import board_read


@pytest.fixture
def history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, priority INTEGER,
            created_at INTEGER, completed_at, tenant TEXT, workflow_template_id TEXT,
            current_step_key TEXT);
        CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
        CREATE INDEX links_parent ON task_links(parent_id);
    """)
    stamps = (100, 200, None, "2026-01-01T00:00:00", "bad", b"300", 200.75)
    conn.executemany("INSERT INTO tasks VALUES (?, 'done', ?, ?, ?, ?, 'flow', 'step')", [
        (f"d{i:04}", i % 10, i, stamps[i % len(stamps)], "one" if i % 2 else "two")
        for i in range(1000)])
    conn.executemany("INSERT INTO tasks VALUES (?, ?, 999, 0, NULL, 'one', 'flow', 'step')", [
        ("waiter", "ready"), ("leaf", "blocked"), ("archived", "archived")])
    conn.executemany("INSERT INTO task_links VALUES (?, 'waiter')", [(f"d{i:04}",) for i in range(1000)])
    conn.execute("INSERT INTO task_links VALUES ('waiter', 'leaf')")
    conn.commit()
    yield conn
    conn.close()


@pytest.mark.parametrize("cap", [0, 3, 10000])
@pytest.mark.parametrize("tenant", [None, "one", "absent"])
def test_done_window_keeps_normalized_ranking_without_dispatch_graph(history, cap, tenant):
    filters = dict(tenant=tenant, include_archived=False,
                   workflow_template_id="flow", current_step_key="step")
    real = board_read.kanban_db.list_task_rows

    def legacy(*args, **kwargs):
        kwargs.pop("order_by", None)
        return real(*args, **kwargs)

    with mock.patch.object(board_read.kanban_db, "list_task_rows", side_effect=legacy):
        expected = board_read._done_state(history, filters, cap)
    recursive = []

    def authorize(action, _a, _b, _database, _trigger):
        if action == sqlite3.SQLITE_RECURSIVE:
            recursive.append(action)
        return sqlite3.SQLITE_OK

    history.set_authorizer(authorize)
    actual = board_read._done_state(history, filters, cap)
    history.set_authorizer(None)
    assert actual == expected
    assert not recursive, "done-window ranking traversed the discarded dispatcher dependency order"
    if tenant != "absent" and cap == 3:
        assert actual[2] == (1000 if tenant is None else 500)
        assert len(actual[1]) == 3
        # ISO time outranks numeric, malformed and NULL stamps after Python coercion.
        assert all(row[0] == "2026-01-01T00:00:00" for row in history.execute(
            f"SELECT completed_at FROM tasks WHERE rowid IN ({','.join('?' * 3)})", actual[1]))
