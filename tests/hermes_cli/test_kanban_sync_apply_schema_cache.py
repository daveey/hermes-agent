"""Feed-page schema discovery must not convoy while holding the board writer."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_sync_apply_schema as schema

from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_sync_apply import _ch, _task_fields


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


def test_large_page_schema_probes_do_not_scale_under_writer(synced_conn, monkeypatch):
    conn = synced_conn
    changes = [_ch(n, 'tasks', f't_schema{n:012d}', _task_fields()) for n in range(1, 257)]
    queries = []
    started = [False]
    apply_one = sync._apply_one

    def apply_row(*args):
        started[0] = True
        return apply_one(*args)

    monkeypatch.setattr(sync, '_apply_one', apply_row)
    conn.set_trace_callback(lambda sql: queries.append((sql, conn.in_transaction and started[0])))
    try:
        result = sync.apply_remote_changes(conn, changes, new_cursor=256, observers=False)
    finally:
        conn.set_trace_callback(None)
    assert result.applied == 256
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_schema%'").fetchone()[0] == 256
    assert sync.sync_state(conn)['cursor'] == 256
    assert sync.sync_state(conn)['applying'] == 0
    assert not sync.outbox(conn)
    probes = [(sql, held) for sql, held in queries
              if 'table_info' in sql.lower() and not sql.lstrip().startswith('--')]
    assert probes, 'schema discovery must actually be exercised'
    assert not any(held for _sql, held in probes), probes
    # One prepared aggregate plus the unchanged two status-origin BEGIN checks.
    assert len(probes) <= 3, 'schema discovery must be page-, not row-sized'


def test_schema_refreshes_migration_between_prepare_and_writer(synced_conn, monkeypatch):
    conn = synced_conn
    prepare = schema._prepare
    seen = []

    def migrate(connection, tables):
        prepared = prepare(connection, tables)
        assert prepared is not None
        seen.append(prepared.version)
        if len(seen) == 1:
            with contextlib.closing(sqlite3.connect(conn.execute('PRAGMA database_list').fetchone()[2])) as writer:
                writer.execute('ALTER TABLE tasks ADD COLUMN remote_extension TEXT')
        return prepared

    monkeypatch.setattr(schema, '_prepare', migrate)
    with schema.page_writer(conn, sync.SYNCED_TABLES, cache=True):
        assert 'remote_extension' in schema.table_columns(conn, 'tasks')
    assert len(seen) == 2
    assert seen[1] > seen[0]
    assert schema._SCHEMA.get() is None


def test_page_failure_rolls_back_rows_cursor_echo_guard_and_scope(synced_conn, monkeypatch):
    from hermes_cli import kanban_db as kb

    conn = synced_conn
    local = kb.create_task(conn, title='pending local edit', assignee='worker')
    pending = sync.outbox(conn)
    assert pending
    before = sync.sync_state(conn)
    apply = sync._apply_one
    calls = []

    def fail_late(*args):
        calls.append(True)
        if len(calls) == 128:
            raise ValueError('injected late page failure')
        return apply(*args)

    monkeypatch.setattr(sync, '_apply_one', fail_late)
    changes = [_ch(1, 'tasks', local, _task_fields(), client_op_id=pending[0]['op_id'])]
    changes.extend(_ch(n, 'tasks', f't_rollback{n:010d}', _task_fields()) for n in range(2, 257))
    with pytest.raises(ValueError, match='injected late page failure'):
        sync.apply_remote_changes(conn, changes, new_cursor=256, observers=False)
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_rollback%'").fetchone()[0] == 0
    assert sync.sync_state(conn) == before
    assert sync.outbox(conn) == pending
    assert schema._SCHEMA.get() is None
    monkeypatch.setattr(sync, '_apply_one', apply)
    recovered = sync.apply_remote_changes(conn, changes, new_cursor=256, observers=False)
    assert recovered.applied == 255
    assert recovered.skipped_echo == 1
    assert pending[0]['op_id'] not in {op['op_id'] for op in sync.outbox(conn)}


@pytest.mark.parametrize('layout', ['attached', 'temp_shadow'])
def test_unusual_resolution_uses_uncached_columns(synced_conn, layout):
    conn = synced_conn
    if layout == 'attached':
        conn.execute("ATTACH DATABASE ':memory:' AS auxiliary")
    else:
        conn.execute('CREATE TEMP TABLE tasks (shadow_column TEXT)')
    with schema.page_writer(conn, sync.SYNCED_TABLES, cache=True):
        assert schema._SCHEMA.get() is None
        if layout == 'temp_shadow':
            assert schema.table_columns(conn, 'tasks') == ['shadow_column']


def test_cached_columns_are_ordered_copied_and_connection_scoped(synced_conn):
    conn = synced_conn
    expected = [row[1] for row in conn.execute('PRAGMA table_info(tasks)')]
    with contextlib.closing(sqlite3.connect(':memory:')) as other:
        other.execute('CREATE TABLE tasks (other_column TEXT)')
        with schema.page_writer(conn, sync.SYNCED_TABLES, cache=True):
            assert schema.table_columns(conn, 'tasks') == expected
            columns = schema.table_columns(conn, 'tasks')
            columns.clear()
            assert schema.table_columns(conn, 'tasks') == expected
            assert schema.table_columns(other, 'tasks') == ['other_column']
        assert schema._SCHEMA.get() is None


def test_large_page_preparation_allows_real_dispatch_tick(synced_conn, kanban_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_model_retirement as retirement
    from hermes_cli import kanban_ratio_routing as ratio

    conn = synced_conn
    support.write_routed_sync_config(kanban_home, True)
    tid = kb.create_task(conn, title='ready during page schema discovery', assignee='default')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    monkeypatch.setattr(retirement, 'sweep', lambda _conn: None)
    monkeypatch.setattr(ratio, 'begin_tick', lambda _conn: None)
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch, 'reap_terminal_workers', lambda _conn: [])
    prepare = schema._prepare
    spawned = []
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    with contextlib.closing(kbc.connect(Path(path))) as dispatcher:
        def interleave(connection, tables):
            prepared = prepare(connection, tables)
            if connection.in_transaction:
                assert spawned == [tid]  # dispatcher schema setup legitimately refreshes the cookie
                return prepared
            result = dispatch.dispatch_once(dispatcher, spawn_fn=lambda task, *_: spawned.append(task.id),
                                            reconcile_orphans=False, max_in_progress=16, max_spawn=1)
            assert [row[0] for row in result.spawned] == [tid], result
            return prepared

        monkeypatch.setattr(schema, '_prepare', interleave)
        changes = [_ch(n, 'tasks', f't_interleave{n:010d}', _task_fields()) for n in range(1, 257)]
        assert sync.apply_remote_changes(conn, changes, new_cursor=256, observers=False).applied == 256
    assert spawned == [tid]
    assert kb.get_task(conn, tid).status == 'running'
    assert sync.sync_state(conn)['cursor'] == 256


def test_keep_callback_uses_uncached_schema(synced_conn, monkeypatch):
    def forbidden(*_args):
        pytest.fail('arbitrary keep callbacks must not use a schema snapshot')

    monkeypatch.setattr(schema, '_prepare', forbidden)
    changes = [_ch(1, 'tasks', 't_keep00000000001', _task_fields())]
    assert sync.apply_remote_changes(synced_conn, changes, new_cursor=1,
                                     keep=lambda *_: True, observers=False).applied == 1
