"""Real SQL regressions for subscription-local invalidation and reader custody."""
import contextlib
import sqlite3
import threading

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_notify_revision as revision
from tui_gateway import notification_idle as idle
from tests.tui_gateway import test_notify_subs_gate as helpers

board = helpers.board
CHAT = helpers.CHAT
_subs_reads = helpers._subs_reads


@contextlib.contextmanager
def writer(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn


@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET title = title || ' changed'",
    "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, 'test', 'comment', 1)",
    "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'heartbeat', '{}', 1)",
    "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'claimed', '{}', 1)",
    "INSERT INTO task_runs(task_id, profile, status, started_at) VALUES (?, 'default', 'running', 1)",
])
def test_unrelated_content_does_not_scan_subscriptions(board, sql):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    with _subs_reads(path) as reads:
        for _ in range(3):
            with writer(path) as conn:
                conn.execute(sql, (_tasks[0],) if '?' in sql else ())
            assert idle.board_signature(path, CHAT) == before
    assert reads == []


def test_private_fallback_does_not_displace_shared_memo(board):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    shared = idle._SIGS[str(path)]
    ready, release = threading.Event(), threading.Event()

    def hold():
        with activity.cached_ro(path):
            ready.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        with writer(path) as conn:
            conn.execute("UPDATE kanban_notify_subs SET last_event_id = 88 WHERE chat_id = ?", (CHAT,))
        for _ in range(3):
            fresh = idle.board_signature(path, CHAT)
            assert fresh != before
            assert fresh[2][0][4] == 88
            assert idle._SIGS[str(path)] is shared
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    with activity.cached_ro(path) as conn:
        sql = []
        conn.set_trace_callback(sql.append)
    try:
        assert idle.board_signature(path, CHAT) == fresh
        assert idle._SIGS[str(path)][0] is shared[0]
        assert not any('WHERE id > 0 ' in s for s in sql), sql
    finally:
        with activity.cached_ro(path) as conn:
            conn.set_trace_callback(None)


@pytest.mark.parametrize('column,value', [
    ('chat_id', 'new-chat'), ('task_id', 'new-task'), ('platform', 'telegram'),
    ('thread_id', 'new-thread'), ('last_event_id', 42), ('inflight_from', 0),
])
def test_every_read_column_invalidates_exact_rows(board, column, value):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    other = idle.board_signature(path, helpers.OTHER)
    with writer(path) as conn:
        conn.execute(f"UPDATE kanban_notify_subs SET {column} = ? WHERE chat_id = ? AND task_id = ?",
                     (value, CHAT, _tasks[0]))
    with _subs_reads(path) as scans:
        after = idle.board_signature(path, CHAT)
    assert len(scans) == 1
    assert after != before
    assert idle.board_signature(path, helpers.OTHER) == other
    with activity.cached_ro(path) as conn:
        expected = tuple(tuple(r) for r in conn.execute(idle._SUBS_SQL) if r[0] == CHAT)
    assert after[2] == expected


@pytest.mark.parametrize('damage', ['missing', 'partial', 'wrong-sql', 'missing-row', 'foreign-schema'])
def test_uncovered_schema_reads_fresh_and_repair_bumps(board, damage):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    with writer(path) as conn:
        old = conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0]
        if damage in ('missing', 'foreign-schema'):
            for name in revision.TRIGGERS:
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute(f"DROP TABLE {revision.TABLE}")
            if damage == 'foreign-schema':
                conn.execute(f"CREATE TABLE {revision.TABLE}(id, revision)")
        elif damage == 'missing-row':
            conn.execute(f"DELETE FROM {revision.TABLE}")
        else:
            conn.execute(f"DROP TRIGGER {revision.PREFIX}update")
            if damage == 'wrong-sql':
                conn.execute(f"CREATE TRIGGER {revision.PREFIX}update AFTER UPDATE ON kanban_notify_subs "
                             "BEGIN SELECT 1; END")
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = 57 WHERE chat_id = ?", (CHAT,))
    with activity.cached_ro(path) as conn:
        assert revision.read_revision(conn, ('damaged',)) is None
    with _subs_reads(path) as scans:
        assert idle.board_signature(path, CHAT) != before
    assert len(scans) == 1
    previous = idle.board_signature(path, CHAT)
    with writer(path) as conn:
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = 58 WHERE chat_id = ?", (CHAT,))
    assert idle.board_signature(path, CHAT) != previous
    with writer(path) as conn:
        schema_before = revision._schema(conn)
        changes_before = conn.total_changes
        revision.ensure_revision(conn)
        if damage == 'foreign-schema':
            assert revision._schema(conn) == schema_before
            assert conn.total_changes == changes_before
            assert revision.read_revision(conn, ('foreign',)) is None
            return
        repaired = conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0]
        assert repaired > (old if damage in ('partial', 'wrong-sql') else 0)
        statements = []
        conn.set_trace_callback(statements.append)
        revision.ensure_revision(conn)
        assert not any(s.startswith(('UPDATE', 'INSERT', 'CREATE', 'DROP')) for s in statements)
    with _subs_reads(path) as scans:
        idle.board_signature(path, CHAT)
    assert len(scans) == 1  # repair revokes the literal fallback publication


def test_generic_trigger_maintenance_leaves_narrow_coverage(board):
    from hermes_cli import kanban_activity_revision as generic
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    with writer(path) as conn:
        generic.drop_triggers(conn)
        generic.ensure_revision(conn, generic.LOCAL_TABLES)
    with _subs_reads(path) as scans:
        assert idle.board_signature(path, CHAT) == before
    assert len(scans) == 1  # schema change invalidates even when rows equal
    with writer(path) as conn:
        conn.execute("UPDATE tasks SET title = 'another title'")
    with _subs_reads(path) as scans:
        assert idle.board_signature(path, CHAT) == before
    assert scans == []


def test_revision_and_rows_share_snapshot(board, monkeypatch):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    original = idle._subs_revision
    injected = []
    snapshots = []

    def commit_after_stamp(conn, path):
        snapshots.append(conn.in_transaction)
        stamp = original(conn, path)
        if not injected:
            injected.append(True)
            with writer(path) as raw:
                raw.execute("UPDATE kanban_notify_subs SET last_event_id = 63 WHERE chat_id = ?", (CHAT,))
        return stamp

    monkeypatch.setattr(idle, '_subs_revision', commit_after_stamp)
    with writer(path) as conn:
        conn.execute("UPDATE tasks SET title = 'force poll'")
    assert idle.board_signature(path, CHAT) == before
    assert injected
    assert snapshots == [True]
    assert idle.board_signature(path, CHAT)[2][0][4] == 63


def test_same_file_new_connection_does_not_reuse_data_version(board):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    old = idle._SIGS[str(path)][0]
    activity.evict_readers(str(path.resolve()))
    with writer(path) as conn:
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = 74 WHERE chat_id = ?", (CHAT,))
    assert idle.board_signature(path, CHAT) != before
    assert idle._SIGS[str(path)][0] is not old


def test_replaced_database_rebuilds_event_and_subscription_state(board):
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    replacement = path.with_name('replacement.db')
    with contextlib.closing(sqlite3.connect(path)) as source:
        with contextlib.closing(sqlite3.connect(replacement)) as target:
            source.backup(target)
    activity.evict_readers(str(path.resolve()))
    with writer(replacement) as conn:
        conn.execute("DELETE FROM task_events")
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = 81 WHERE chat_id = ?", (CHAT,))
    replacement.replace(path)
    assert idle.board_signature(path, CHAT) != before
    assert idle.board_signature(path, CHAT)[2][0][4] == 81


def test_deleted_wal_generation_discards_signature_reader(board):
    from pathlib import Path
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    old = idle._SIGS[str(path)][0]
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    Path(str(path) + '-wal').unlink(missing_ok=True)
    Path(str(path) + '-shm').unlink(missing_ok=True)
    assert idle.board_signature(path, CHAT) == before
    assert idle._SIGS[str(path)][0] is not old


def test_unknown_reader_has_no_shared_custody(tmp_path):
    from hermes_cli.kanban_reader_lifecycle import is_shared_reader
    with contextlib.closing(sqlite3.connect(':memory:')) as conn:
        assert not is_shared_reader(tmp_path / 'unknown.db', conn)


def test_install_failure_preserves_literal_fallback(board, monkeypatch, caplog):
    from hermes_cli import kanban_db_connect as connect
    path, _tasks = board
    with writer(path) as conn:
        for name in revision.TRIGGERS:
            conn.execute(f'DROP TRIGGER {name}')
    def fail(conn):
        raise sqlite3.OperationalError('controlled install refusal')
    monkeypatch.setattr(revision, 'ensure_revision', fail)
    with writer(path) as conn:
        connect._ensure_activity_revision(conn, str(path))
        assert revision.read_revision(conn, ('failed',)) is None
    assert 'notification reads stay literal' in caplog.text


def test_old_schema_install_is_read_only(tmp_path):
    with contextlib.closing(sqlite3.connect(tmp_path / 'old.db')) as conn:
        conn.execute('CREATE TABLE kanban_notify_subs(task_id)')
        revision.ensure_revision(conn)
        assert revision.read_revision(conn, ('old',)) is None
        assert conn.execute('SELECT name FROM sqlite_master WHERE name = ?', (revision.TABLE,)).fetchone() is None


def test_repair_is_atomic_and_removes_extra_owned_trigger(board):
    path, _tasks = board
    with writer(path) as conn:
        conn.execute(f"DROP TRIGGER {revision.PREFIX}update")
        conn.execute(f"CREATE TRIGGER {revision.PREFIX}extra AFTER UPDATE ON kanban_notify_subs BEGIN SELECT 1; END")
    with contextlib.closing(sqlite3.connect(path)) as conn:
        old_schema = revision._schema(conn)
        old_revision = conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0]
        conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER
                            else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            revision.ensure_revision(conn)
        conn.set_authorizer(None)
        assert revision._schema(conn) == old_schema
        assert conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0] == old_revision
        revision.ensure_revision(conn)
        assert revision._schema(conn) == {revision.TABLE: revision.DDL, **revision.TRIGGERS}


def test_missing_counter_repair_cannot_reuse_an_old_revision(board):
    path, _tasks = board
    with writer(path) as conn:
        conn.execute(f"UPDATE {revision.TABLE} SET revision = 1")
    before = idle.board_signature(path, CHAT)
    with writer(path) as conn:
        conn.execute(f"DELETE FROM {revision.TABLE}")
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = 92 WHERE chat_id = ?", (CHAT,))
    with writer(path) as conn:
        revision.ensure_revision(conn)
        assert conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0] == 1
    assert idle.board_signature(path, CHAT) != before


def test_unchanged_and_unread_columns_do_not_bump(board):
    path, _tasks = board
    idle.board_signature(path, CHAT)
    with writer(path) as conn:
        start = conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0]
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = last_event_id, inflight_at = 123")
        assert conn.execute(f"SELECT revision FROM {revision.TABLE}").fetchone()[0] == start
    with _subs_reads(path) as scans:
        idle.board_signature(path, CHAT)
    assert scans == []
