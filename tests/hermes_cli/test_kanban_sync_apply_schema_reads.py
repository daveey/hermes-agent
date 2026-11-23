"""A sync page apply never reads a schema one row per column.

apply_remote_changes holds the board write lock for the whole page. Each change re-ran PRAGMA table_info (59 rows
for tasks) several times, plus the containment guard's probes; in the hub gateway every fetched row re-takes the
GIL, so the lock was held ~1 s per change and the dispatcher, claims and notifications starved (2026-11-23).
Schema reads must be the one-row ``pragma_table_info`` form (kanban_table_columns).
"""
from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_db_connect as db
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support

ME = "macbook"


def _changes(n, start=0):
    return [sync.RemoteChange(start + i + 1, "tasks", f"t_remote_{start + i}", "upsert",
                              {"created_on": "worker-a2", "title": f"remote {start + i}", "status": "todo",
                               "created_at": 1_795_000_000_000},
                              1, "", "worker-a2") for i in range(n)]


def _row_per_column_reads(conn, changes, cursor):
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        sync.apply_remote_changes(conn, changes, new_cursor=cursor, observers=False)
    finally:
        conn.set_trace_callback(None)
    return sum(1 for sql in seen if sql.lstrip().upper().startswith("PRAGMA TABLE_INFO"))


def test_row_per_column_schema_reads_do_not_grow_with_page_size(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = db.connect(home / "kanban.db")
    try:
        from hermes_cli.kanban_sync_client import ensure_identity
        ensure_identity(conn, "default", ME)
        small = _row_per_column_reads(conn, _changes(2), 2)
        large = _row_per_column_reads(conn, _changes(40, start=100), 200)
        assert large == small, (small, large)
        titles = {r[0] for r in conn.execute("SELECT title FROM tasks WHERE id LIKE 't_remote_%'")}
        assert {"remote 0", "remote 1", "remote 100", "remote 139"} <= titles
    finally:
        conn.close()


def test_table_columns_keeps_order_and_sees_ddl(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    conn = db.connect(home / "kanban.db")
    try:
        legacy = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        assert sync.table_columns(conn, "tasks") == legacy
        assert sync.table_columns(conn, "no_such_table") == []
        conn.execute("ALTER TABLE task_links ADD COLUMN memo_probe TEXT")
        assert sync.table_columns(conn, "task_links")[-1] == "memo_probe"
    finally:
        conn.close()


def test_containment_pending_reads_schemas_in_one_row(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    conn = db.connect(home / "kanban.db")
    try:
        seen = []
        conn.set_trace_callback(seen.append)
        guard.pending(conn, "t_absent")
        conn.set_trace_callback(None)
        assert not [sql for sql in seen if sql.lstrip().upper().startswith("PRAGMA TABLE_INFO")]
    finally:
        conn.close()
