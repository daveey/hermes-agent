"""Outbox capture for synced boards (core patch 0193): config gate, trigger
coverage of every synced column, the trigger-drift guard, tombstoned deletes
and wide task ids. Design: hermes-mods ``core/kanban-central-service.md``."""

from __future__ import annotations

import re
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli.config_defaults import DEFAULT_CONFIG
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


def _triggers(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                       "AND name LIKE 'kanban_sync_%'")}


def _ops(conn) -> list[dict]:
    return sync.outbox(conn)


def _clear_outbox(conn) -> None:
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")


def test_config_key_turns_capture_on_through_the_loader(kanban_home):
    assert DEFAULT_CONFIG["kanban"]["sync"]["enabled"] is False
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "on.db")
    try:
        assert {"kanban_sync_tasks_ins", "kanban_sync_tasks_upd", "kanban_sync_tasks_del"} <= _triggers(conn)
        assert conn.execute("SELECT mode FROM kanban_sync_state").fetchone()[0] == "shadow"
        kb.create_task(conn, title="captured")
        assert [op["table_name"] for op in _ops(conn)][:1] == ["tasks"]
    finally:
        conn.close()


def test_flag_off_installs_nothing_and_drops_old_triggers(kanban_home):
    conn = kbc.connect(kanban_home / "off.db")
    try:
        assert _triggers(conn) == set()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'kanban_sync_outbox'").fetchone() is None
        assert re.fullmatch(r"t_[0-9a-f]{8}", kb.create_task(conn, title="classic"))
    finally:
        conn.close()
    support.write_sync_config(kanban_home, True)
    kbc.init_db(kanban_home / "off.db")
    with kbc.connect_closing(kanban_home / "off.db") as conn:
        assert _triggers(conn)
    support.write_sync_config(kanban_home, False)
    kbc.init_db(kanban_home / "off.db")
    with kbc.connect_closing(kanban_home / "off.db") as conn:
        assert _triggers(conn) == set()
        kb.create_task(conn, title="not captured")
        assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0


def _probe_value(conn, table: str, column: str, run_id: int):
    if (table, column) in sync.REF_COLUMNS:
        return run_id
    decl = next(r[2] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] == column).upper()
    return 7_000_001 if "INT" in decl else f"probe-{column}"


def _seed_rows(conn) -> dict[str, int | str]:
    parent = kb.create_task(conn, title="parent")
    tid = kb.create_task(conn, title="child", assignee="worker")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "me", "hello")
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO task_attachments (task_id, filename, stored_path, created_at) "
                     "VALUES (?, 'f.txt', '/tmp/f.txt', 1)", (tid,))
    run_id = kb._current_run_id(conn, tid)
    return {
        "parent": parent, "tasks": tid, "run_id": run_id,
        "task_comments": conn.execute("SELECT MAX(id) FROM task_comments").fetchone()[0],
        "task_events": conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0],
        "task_runs": run_id,
        "task_attachments": conn.execute("SELECT MAX(id) FROM task_attachments").fetchone()[0],
    }


def test_every_synced_column_is_captured_on_update(synced_conn):
    conn = synced_conn
    rows = _seed_rows(conn)
    run_uid = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (rows["run_id"],)).fetchone()[0]
    for table in ("tasks", "task_comments", "task_events", "task_runs", "task_attachments"):
        columns = sync.captured_columns(conn, table)
        assert sync.SYNCED_COLUMNS[table] <= set(columns)
        for column in columns:
            if (table, column) in sync.REF_COLUMNS:
                with kbc.write_txn(conn):
                    conn.execute(f"UPDATE {table} SET {column} = NULL WHERE id = ?", (rows[table],))
            _clear_outbox(conn)
            value = _probe_value(conn, table, column, rows["run_id"])
            with kbc.write_txn(conn):
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, rows[table]))
            ops = _ops(conn)
            wire = sync.REF_COLUMNS.get((table, column), column)
            expected = run_uid if (table, column) in sync.REF_COLUMNS else value
            assert [(op["table_name"], op["op"], op["fields"]) for op in ops] == [(table, "upsert", {wire: expected})], (
                table, column)


def test_insert_captures_every_synced_column(synced_conn):
    conn = synced_conn
    _clear_outbox(conn)
    tid = kb.create_task(conn, title="fresh")
    kb.add_comment(conn, tid, "me", "body")
    kb.link_tasks(conn, kb.create_task(conn, title="p"), tid)
    by_table: dict[str, dict] = {}
    for op in _ops(conn):
        by_table.setdefault(op["table_name"], op)
    for table, op in by_table.items():
        wire = {sync.REF_COLUMNS.get((table, c), c) for c in sync.captured_columns(conn, table)}
        assert set(op["fields"]) == wire, table
    assert by_table["task_links"]["row_uid"].endswith(f">{tid}")
    assert by_table["tasks"]["row_uid"] == tid


def test_local_only_columns_are_never_captured(synced_conn):
    conn = synced_conn
    rows = _seed_rows(conn)
    for table in ("tasks", "task_runs", "task_attachments"):
        for column in sorted(sync.LOCAL_COLUMNS[table]):
            _clear_outbox(conn)
            with kbc.write_txn(conn):
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?",
                             (_probe_value(conn, table, column, rows["run_id"]), rows[table]))
            assert _ops(conn) == [], (table, column)


def test_heartbeat_writes_no_outbox_rows(synced_conn):
    conn = synced_conn
    tid = kb.create_task(conn, title="beat", assignee="worker")
    kb.claim_task(conn, tid)
    _clear_outbox(conn)
    assert kb.heartbeat_claim(conn, tid)
    assert _ops(conn) == []


def test_blob_cells_are_captured_without_failing_the_write(synced_conn):
    """JSON cannot hold a BLOB; the trigger must encode it, never fail the writer's statement."""
    conn = synced_conn
    tid = kb.create_task(conn, title="blobby")
    _clear_outbox(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (b"\xff\xfe raw", tid))
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', ?, 1)",
                     (tid, b"\x00\x01"))
    fields = [op["fields"] for op in _ops(conn)]
    assert fields[0] == {"body": {"$blob": "FFFE20726177"}}
    assert fields[1]["body"] == {"$blob": "0001"}
    other = kb.create_task(conn, title="target")
    sync.apply_remote_changes(conn, [sync.RemoteChange(1, "tasks", other, "upsert", fields[0], 1, "", "gw_x")],
                              new_cursor=1)
    assert conn.execute("SELECT body FROM tasks WHERE id = ?", (other,)).fetchone()[0] == b"\xff\xfe raw"


def test_every_task_column_is_declared_synced_or_local(synced_conn):
    """Trigger drift guard: a column an upstream migration adds must be put in
    SYNCED_COLUMNS or LOCAL_COLUMNS of ``kanban_db_sync`` deliberately."""
    assert sync.undeclared_columns(synced_conn) == {}


def test_drift_guard_names_a_new_column_and_capture_includes_it(synced_conn):
    conn = synced_conn
    with kbc.write_txn(conn):
        conn.execute("ALTER TABLE tasks ADD COLUMN upstream_new_field TEXT")
    assert sync.undeclared_columns(conn) == {"tasks": {"upstream_new_field"}}
    sync.install_triggers(conn)
    tid = kb.create_task(conn, title="drift")
    _clear_outbox(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET upstream_new_field = 'x' WHERE id = ?", (tid,))
    assert [op["fields"] for op in _ops(conn)] == [{"upstream_new_field": "x"}]


def test_delete_task_writes_one_tombstone_and_drops_later_pulls(synced_conn):
    conn = synced_conn
    parent = kb.create_task(conn, title="parent")
    tid = kb.create_task(conn, title="doomed", parents=[parent])
    kb.add_comment(conn, tid, "me", "bye")
    _clear_outbox(conn)
    assert kb.delete_task(conn, tid)
    ops = [(op["table_name"], op["op"], op["row_uid"]) for op in _ops(conn) if op["op"] == "delete"]
    assert ops == [("tasks", "delete", tid)]  # links/comments/events ride on the task's tombstone
    assert conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name = 'tasks' AND row_uid = ?",
                        (tid,)).fetchone()
    late = [
        sync.RemoteChange(1, "tasks", tid, "upsert", {"title": "revived", "status": "ready", "created_at": 1},
                          5, "", "gw_other"),
        sync.RemoteChange(2, "task_comments", "c-late", "upsert",
                          {"task_id": tid, "author": "x", "body": "late", "created_at": 1}, 1, "", "gw_other"),
    ]
    result = sync.apply_remote_changes(conn, late, new_cursor=2)
    assert result.applied == 0 and result.skipped == 2
    assert kb.get_task(conn, tid) is None
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_synced_board_mints_wide_time_ordered_task_ids(synced_conn):
    before_ms = time.time_ns() // 1_000_000
    tid = kb.create_task(synced_conn, title="wide")
    assert re.fullmatch(r"t_[0-9a-f]{16}", tid)
    assert abs(int(tid[2:14], 16) - before_ms) < 60_000
    assert kb._TASK_ID_PROSE_RE.search(f"see {tid} for details")


def test_rows_written_before_enable_get_uids(kanban_home):
    with kbc.connect_closing(kanban_home / "late.db") as conn:
        tid = kb.create_task(conn, title="old", assignee="worker")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "me", "before sync")
    support.write_sync_config(kanban_home, True)
    kbc.init_db(kanban_home / "late.db")
    with kbc.connect_closing(kanban_home / "late.db") as conn:
        for table in ("task_comments", "task_events", "task_runs"):
            uids = [r[0] for r in conn.execute(f"SELECT uid FROM {table}")]
            assert uids and None not in uids and len(set(uids)) == len(uids), table
