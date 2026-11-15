"""The board-wide starvation clock must not scan historical runs. [daveey]"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


INDEX = "idx_runs_started_at"
PREDICATE = "(started_at+0 IS NULL OR started_at+0 IS NOT NULL)"
MAX_SQL = f"SELECT MAX(started_at) FROM task_runs WHERE {PREDICATE}"


@pytest.mark.parametrize("analyze", [False, True])
def test_index_is_only_eligible_for_explicit_starvation_predicate(analyze):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(kb.SCHEMA_SQL)
        kbc._migrate_add_optional_columns(conn)
        conn.executemany("INSERT INTO task_runs(task_id,status,started_at) VALUES (?, 'done', ?)",
                         [(f"t{i % 10}", 1000-i) for i in range(1000)])
        if analyze:
            conn.execute("ANALYZE")
        forced = f"SELECT started_at FROM task_runs INDEXED BY {INDEX}"
        for suffix in ("", " WHERE started_at < 5", " WHERE started_at >= 5",
                       " WHERE started_at IS NULL", " WHERE started_at IS NOT NULL",
                       " WHERE task_id='t1' AND started_at >= 5", " ORDER BY started_at"):
            with pytest.raises(sqlite3.OperationalError, match="no query solution"):
                conn.execute(forced + suffix).fetchall()
        assert conn.execute(forced + f" WHERE {PREDICATE}").fetchall()
        plan = str([tuple(r) for r in conn.execute("EXPLAIN QUERY PLAN " + MAX_SQL)])
        assert f"COVERING INDEX {INDEX}" in plan


def test_upgrade_global_experimental_index_and_settled_noop():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (started_at INTEGER)")
        conn.execute(f"CREATE INDEX {INDEX} ON task_runs(started_at)")
        kbc._ensure_run_started_index(conn)
        assert PREDICATE in _index_sql(conn)
        cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
        kbc._ensure_run_started_index(conn)
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == cookie


def test_served_at_missing_index_and_null_fallback():
    from hermes_cli.kanban_board_share import _served_at
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (started_at INTEGER)")
        assert _served_at(conn) == 0
        conn.executemany("INSERT INTO task_runs VALUES (?)", [(None,), (12,), (-5,)])
        assert _served_at(conn) == 12
        kbc._ensure_run_started_index(conn)
        assert _served_at(conn) == 12


def _index_sql(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (INDEX,)).fetchone()
    assert row is not None
    return row[0].replace("IF NOT EXISTS ", "")


def test_fresh_additive_and_rebuild_index_agree():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(kb.SCHEMA_SQL)
        kbc._migrate_add_optional_columns(conn)
        conn.commit()
        fresh = _index_sql(conn)
        conn.execute(f"DROP INDEX {INDEX}")
        kbc._migrate_add_optional_columns(conn)
        assert _index_sql(conn) == fresh
        # Retention/schema rebuild recreates precisely the canonical run table.
        conn.execute("DROP TABLE task_runs")
        table, indexes = kbc._REBUILD_SPECS["task_runs"]
        conn.execute(table)
        for ddl in indexes:
            conn.execute(ddl)
        assert _index_sql(conn) == fresh
        conn.commit()
        kbc._migrate_add_optional_columns(conn)
        assert _index_sql(conn) == fresh


def test_legacy_missing_started_at_is_guarded():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY)")
        kbc._ensure_run_started_index(conn)
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone() is None
        conn.execute("ALTER TABLE task_runs ADD COLUMN started_at INTEGER")
        kbc._ensure_run_started_index(conn)
        kbc._ensure_run_started_index(conn)
        assert "started_at" in _index_sql(conn)


@pytest.mark.parametrize("values", [[], [None], [None, None], [4, 99, -5, 2], [8, 8, None], [2, "late", b"last", None]])
def test_indexed_max_matches_scan_including_storage_classes(values):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (started_at INTEGER)")
        conn.executemany("INSERT INTO task_runs VALUES (?)", [(v,) for v in values])
        before = tuple(conn.execute("SELECT MAX(started_at), typeof(MAX(started_at)) FROM task_runs").fetchone())
        kbc._ensure_run_started_index(conn)
        for analyze in (False, True):
            if analyze:
                conn.execute("ANALYZE")
            assert tuple(conn.execute(
                "SELECT MAX(started_at), typeof(MAX(started_at)) FROM task_runs WHERE " + PREDICATE
            ).fetchone()) == before
            plan = str([tuple(r) for r in conn.execute("EXPLAIN QUERY PLAN " + MAX_SQL)])
            assert f"COVERING INDEX {INDEX}" in plan


def test_max_vm_work_is_bounded_with_real_history():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(kb.SCHEMA_SQL)
        kbc._migrate_add_optional_columns(conn)
        conn.executemany("INSERT INTO task_runs(task_id,status,started_at) VALUES (?, 'done', ?)",
                         [(f"task-{i % 1000}", i % 19999) for i in range(80000)])
        steps = []
        conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
        from hermes_cli.kanban_board_share import _served_at
        assert _served_at(conn) == 19998
        conn.set_progress_handler(None, 0)
        assert len(steps) < 100, len(steps)
