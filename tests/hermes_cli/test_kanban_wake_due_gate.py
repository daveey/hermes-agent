"""The due index gates probe selection, never recovery or pool housekeeping. [daveey]"""
from concurrent.futures import Future
from unittest.mock import Mock

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_wake_check as helpers

kanban_home = helpers.kanban_home


def test_deadline_query_uses_covering_index_after_schema_upgrade(kanban_home):
    with kbc.connect_closing() as conn:
        # Simulate an existing board from before the deadline index was added.
        conn.execute("DROP INDEX IF EXISTS idx_wake_checks_next_at")
        kbc.apply_schema_steps(conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT MIN(next_at) FROM task_wake_checks"
        ).fetchall()
        assert any("COVERING INDEX idx_wake_checks_next_at" in row[3] for row in plan)
        # Schema initialization remains idempotent on subsequent opens.
        kbc.apply_schema_steps(conn)
        assert conn.execute("SELECT MIN(next_at) FROM task_wake_checks").fetchone()[0] is None


def test_not_due_avoids_probe_join_and_config_but_due_runs(kanban_home, monkeypatch):
    from hermes_cli import kanban_ci_progress as ci
    monkeypatch.setattr(ci, "submit_sweep", Mock(return_value=None))
    stale = Mock(return_value=10000)
    monkeypatch.setattr(kwc.episode, "stale_seconds", stale)
    pool = Mock()
    completed = Future()
    completed.set_result(None)
    pool.submit.return_value = completed
    monkeypatch.setattr(kwc, "_pool", lambda: pool)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="due probe", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
            kwc.set_check(conn, tid, "true", 300, now=1000)
            conn.execute("UPDATE task_wake_checks SET next_at=1100 WHERE task_id=?", (tid,))
        sql = []
        conn.set_trace_callback(sql.append)
        for now in (1000, 1099):
            assert kwc.run_due_wake_checks(conn, now=now)["submitted"] == []
        assert stale.call_count == 0
        assert not any("SELECT w.task_id, w.cmd, w.every" in q for q in sql)
        # No task/probe writes intervene: advancing to the indexed deadline is enough.
        assert kwc.run_due_wake_checks(conn, now=1100)["submitted"] == [tid]
        stale.assert_called_once()
        pool.submit.assert_called_once()
        assert kwc.get_state(conn, tid)["next_at"] == 1400
        conn.set_trace_callback(None)
