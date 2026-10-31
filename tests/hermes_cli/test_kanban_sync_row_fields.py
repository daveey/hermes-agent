"""A sync round must not rediscover the schema for every already-materialized row."""
import sqlite3

import pytest

from hermes_cli import kanban_sync_client as client


def test_row_fields_reuses_row_metadata_without_sql():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id, title, body, worker_pid, created_on, future_field)")
        conn.executemany("INSERT INTO tasks VALUES (?, 'title', ?, 12, 'gw', 'private')",
                         [(str(i), b"body") for i in range(100)])
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        statements = []
        conn.set_trace_callback(statements.append)
        for row in rows:
            assert client._row_fields(conn, "tasks", row) == {"title": "title", "body": {"$blob": "626f6479"}}
        assert statements == []
        # No process-global schema cache may hide newly present synced columns.
        conn.execute("ALTER TABLE tasks ADD COLUMN status DEFAULT 'ready'")
        row = conn.execute("SELECT * FROM tasks LIMIT 1").fetchone()
        statements.clear()
        assert client._row_fields(conn, "tasks", row)["status"] == "ready"
        assert statements == []


@pytest.mark.parametrize("table,ref,wire", [("tasks", "current_run_id", "current_run_uid"),
                                           ("task_events", "run_id", "run_uid")])
@pytest.mark.parametrize("value,expected", [(1, "run-one"), (2, None), (None, None)])
def test_row_fields_keeps_reference_resolution(table, ref, wire, value, expected):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (id, uid)")
        conn.execute("INSERT INTO task_runs VALUES (1, 'run-one')")
        conn.execute(f"CREATE TABLE {table} (id, {ref})")
        conn.execute(f"INSERT INTO {table} VALUES ('row-one', ?)", (value,))
        row = conn.execute(f"SELECT * FROM {table}").fetchone()
        statements = []
        conn.set_trace_callback(statements.append)
        assert client._row_fields(conn, table, row) == {wire: expected}
        assert len(statements) == (0 if value is None else 1)
        assert all(s.startswith("SELECT uid FROM task_runs") for s in statements)
