"""Local cleanup evidence is transactional and never substitutes for an open run."""
import sqlite3

from hermes_cli.kanban_run_tombstones import ended_run, retain_ended_runs


def test_legacy_missing_and_open_rows_are_not_ended():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, worker_pid INTEGER)")
        assert ended_run(conn, "t", 1) is None
        conn.execute("INSERT INTO task_runs VALUES (1, 't', NULL, NULL)")
        retain_ended_runs(conn, "t")
        assert conn.execute("SELECT count(*) FROM kanban_ended_run_tombstones").fetchone()[0] == 0
        conn.execute("UPDATE task_runs SET ended_at=100")
        retain_ended_runs(conn, "t")
        conn.execute("UPDATE task_runs SET ended_at=NULL")
        assert ended_run(conn, "t", 1) == (None, None)
        conn.execute("DELETE FROM task_runs")
        assert ended_run(conn, "t", 1) == (100, None)
        assert ended_run(conn, "other", 1) is None


def test_retention_rolls_back_with_eviction(tmp_path):
    path = tmp_path / "board.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, worker_pid INTEGER)")
        conn.execute("INSERT INTO task_runs VALUES (1, 't', 100, NULL)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        retain_ended_runs(conn, "t")
        conn.execute("DELETE FROM task_runs")
        conn.rollback()
        assert ended_run(conn, "t", 1) == (100, None)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_ended_run_tombstones'").fetchone()
        with conn:
            retain_ended_runs(conn, "t")
            conn.execute("DELETE FROM task_runs")
    with sqlite3.connect(path) as reopened:
        assert ended_run(reopened, "t", 1) == (100, None)
