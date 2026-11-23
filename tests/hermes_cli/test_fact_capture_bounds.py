"""Bound source reads before materializing dirty keys. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_task_change as changes
from hermes_cli.fact_capture import CaptureBudget, capture_tasks
from tests.hermes_cli import test_fact_capture as capture_helpers
from tests.hermes_cli import test_kanban_task_change as change_helpers

home = change_helpers.home
board = change_helpers.board
shadow = change_helpers.shadow
capture_board = capture_helpers.board


@pytest.mark.parametrize("source", ["board", "shadow"])
def test_dirty_key_read_stops_at_limit_plus_sentinel(source, request):
    writer = request.getfixturevalue(source)
    _, cursor = changes.changed_since(writer, None)
    keys = [kb.create_task(writer, title=str(i), assignee="w") for i in range(8)]
    returned = []
    original = writer.row_factory

    def counted(sql_cursor, row):
        if tuple(column[0] for column in sql_cursor.description) == ("task_id",):
            returned.append(row)
        return original(sql_cursor, row) if original else row

    writer.row_factory = counted
    try:
        dirty, after = changes.changed_since(writer, cursor, limit=2)
        assert dirty is changes.FULL
        assert after.revision > cursor.revision
        assert len(returned) == 3
        returned.clear()
        assert changes.changed_since(writer, after, limit=2)[0] == frozenset()
        assert returned == []
        with kbc.write_txn(writer):
            writer.execute("UPDATE tasks SET title='changed' WHERE id=?", (keys[-1],))
        assert changes.changed_since(writer, after, limit=2)[0] == {keys[-1]}
    finally:
        writer.row_factory = original


def test_capture_passes_budget_to_dirty_read(capture_board, monkeypatch):
    path, writer = capture_board
    key = kb.create_task(writer, title="one", assignee="w")
    original = changes.changed_since
    limits = []

    def bounded(conn, cursor, **kwargs):
        limits.append(kwargs.get("limit"))
        return original(conn, cursor, **kwargs)

    monkeypatch.setattr(changes, "changed_since", bounded)
    captured = capture_tasks(path, None, capture_helpers.rows, budget=CaptureBudget(max_keys=1))
    assert captured.ids == (key,)
    assert limits == [1, 1]


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_cursor_limit_is_refused(board, limit):
    with pytest.raises(ValueError, match="limit"):
        changes.changed_since(board, None, limit=limit)


def test_aggregate_bytes_refuse_without_leaking_handle(capture_board):
    import sqlite3
    from hermes_cli.fact_capture import CaptureRefused
    path, writer = capture_board
    for title in ("a", "b"):
        kb.create_task(writer, title=title, assignee="w")
    borrowed = []
    def large(conn, ids):
        borrowed.append(conn)
        for key in ids:
            yield {"id": key, "payload": "x" * 20000}
    with pytest.raises(CaptureRefused, match="byte"):
        capture_tasks(path, None, large, budget=CaptureBudget(max_bytes=32768))
    with pytest.raises(sqlite3.ProgrammingError):
        borrowed[0].execute("SELECT 1")
    good = capture_tasks(path, None, capture_helpers.rows)
    assert good.full and 0 < good.payload_bytes < 32768


def test_sqlite_rejects_oversized_row_before_hydration(capture_board):
    import sqlite3
    path, writer = capture_board
    kb.create_task(writer, title="a", assignee="w")
    returned = []
    def huge(conn, ids):
        for row in conn.execute("SELECT zeroblob(65536)"):
            returned.append(row)
            yield {"value": row[0]}
    with pytest.raises(sqlite3.DataError):
        capture_tasks(path, None, huge, budget=CaptureBudget(max_bytes=32768))
    assert returned == []


def test_hydration_batches_never_exceed_500(capture_board):
    path, writer = capture_board
    with kbc.write_txn(writer):
        writer.executemany("INSERT INTO tasks(id,title,assignee,status,created_at) VALUES(?,?,'w','ready',1)",
                           [(f"bounded-{i:04}", str(i)) for i in range(501)])
    batches = []
    def hydrated(conn, ids):
        batches.append(len(ids))
        query = "SELECT id,title FROM tasks WHERE id IN (" + ",".join("?" * len(ids)) + ")"
        return ({"id": row[0], "title": row[1]} for row in conn.execute(query, ids))
    result = capture_tasks(path, None, hydrated)
    assert batches == [500, 1]
    assert len(result.records) == 501
