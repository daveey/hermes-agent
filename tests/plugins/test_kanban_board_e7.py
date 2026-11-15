"""Stage7 removes repeated SQL/projection work without weakening exact reads."""
import json
import sqlite3
from types import MappingProxyType

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd
from plugins.kanban.dashboard import board_incremental as bi
from plugins.kanban.dashboard import board_read as br
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


def test_episode_payload_is_decoded_once_but_clock_remains_live(monkeypatch):
    events = kd.EventHistory([{'id': 1, 'kind': 'dispatch_skipped', 'created_at': 10,
                               'payload': json.dumps({'reason': 'pinned_quota_wall', 'until': 100})}])
    calls = []
    original = kd._parse_payload
    def parse(event):
        calls.append(event)
        return original(event)
    monkeypatch.setattr(kd, '_parse_payload', parse)
    assert kd._pinned_quota_hold_until(events, 99) == 100
    assert kd._pinned_quota_hold_until(events, 100) is None
    assert kd._pinned_quota_hold_until(events, 90) == 100
    assert len(calls) == 1
    newer = kd.EventHistory([*events, {'id': 2, 'kind': 'claimed', 'created_at': 11, 'payload': '{}'}])
    assert kd._episode_skip(newer) == kd._episode_skip(tuple(newer))


def test_proxy_identity_does_not_hide_mutable_backing(monkeypatch):
    source = {'ended_at': 10}
    runs = [MappingProxyType(source)]
    events = kd.EventHistory([])
    monkeypatch.setattr(kd, '_last_ready_entry_ts', lambda _e, r: r[0]['ended_at'])
    assert events.ready_floor(runs) == 10
    source['ended_at'] = 20
    assert events.ready_floor(runs) == 20


def test_incremental_fetch_avoids_dispatch_priority_walk(board, monkeypatch):
    ids, _ = helpers._seed()
    original = kb.list_task_rows
    orders = []
    def read(conn, **kwargs):
        orders.append(kwargs.get('order_by'))
        return original(conn, **kwargs)
    monkeypatch.setattr(kb, 'list_task_rows', read)
    conn = kbc.connect()
    try:
        result = bi._fetch(conn, {}, ids)
        assert set(result) == set(ids)
        assert orders == ['created']
    finally:
        conn.close()


@pytest.mark.parametrize('column,value', [('last_heartbeat_at', 123), ('worker_pid', 321),
                                         ('claim_expires', 222), ('title', 'changed')])
def test_delta_reads_only_moved_wide_rows(board, monkeypatch, column, value):
    ids, _ = helpers._seed()
    helpers._read(board)
    fetched = []
    original = bi._fetch
    def fetch(conn, filters, ids):
        fetched.extend(ids)
        return original(conn, filters, ids)
    monkeypatch.setattr(bi, '_fetch', fetch)
    conn = kbc.connect()
    try:
        conn.execute(f'UPDATE tasks SET {column} = ? WHERE id = ?', (value, ids[0]))
        conn.commit()
        result = helpers._stable(helpers._read(board))
        assert set(fetched) == {ids[0]}
        assert result == helpers._cold(board)
    finally:
        conn.close()


def test_run_covering_index_upgrades_and_settles():
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, claim_lock TEXT, '
                     'claim_expires INTEGER, last_heartbeat_at INTEGER, worker_pid INTEGER, worker_started_at INTEGER)')
        kbc.ensure_run_bookkeeping_index(conn)
        conn.execute('ALTER TABLE task_runs ADD COLUMN sync_version INTEGER')
        kbc.ensure_run_bookkeeping_index(conn)
        columns = [row[2] for row in conn.execute('PRAGMA index_info(idx_runs_task_bookkeeping)')]
        assert columns == ['task_id', 'id', 'claim_lock', 'claim_expires', 'last_heartbeat_at',
                           'worker_pid', 'worker_started_at', 'sync_version']
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        kbc.ensure_run_bookkeeping_index(conn)
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == version
        plan = conn.execute('EXPLAIN QUERY PLAN SELECT task_id,id,claim_lock,claim_expires,last_heartbeat_at,'
                            'worker_pid,worker_started_at,sync_version FROM task_runs '
                            "WHERE task_id IN ('a','b') ORDER BY task_id,id").fetchall()
        assert any('COVERING INDEX idx_runs_task_bookkeeping' in row[3] for row in plan)
    finally:
        conn.close()


def test_overlay_without_retained_rows_uses_exact_fetch(board):
    ids, _ = helpers._seed()
    conn = kbc.connect()
    try:
        result = bi._bookkeeping_overlay(conn, {}, dict.fromkeys(ids), [], None)
        assert {k: tuple(v) for k, v in result.items()} == {
            k: tuple(v) for k, v in bi._fetch(conn, {}, ids).items()}
    finally:
        conn.close()


def test_schema_change_and_deleted_row_match_full_read(board):
    ids, _ = helpers._seed()
    helpers._read(board)
    conn = kbc.connect()
    try:
        conn.execute('ALTER TABLE tasks ADD COLUMN e7_new_column TEXT')
        conn.execute('DELETE FROM tasks WHERE id = ?', (ids[-1],))
        conn.commit()
        assert helpers._stable(helpers._read(board)) == helpers._cold(board)
    finally:
        conn.close()


def test_sync_schema_installs_same_covering_index(board):
    from hermes_cli import kanban_db_sync
    conn = kbc.connect()
    try:
        kanban_db_sync.apply_sync_schema_steps(conn)
        columns = [r[2] for r in conn.execute('PRAGMA index_info(idx_runs_task_bookkeeping)')]
        assert 'sync_version' in columns
        before = conn.execute('PRAGMA schema_version').fetchone()[0]
        kbc.ensure_run_bookkeeping_index(conn)
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == before
    finally:
        conn.close()


def test_unmarked_event_marks_preserve_duplicates_and_empty_tasks():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT)')
        from hermes_cli.kanban_activity_revision import UNREAD_EVENT_KINDS
        kind = next(iter(UNREAD_EVENT_KINDS))
        conn.executemany('INSERT INTO task_events VALUES (?,?,?)', [(1, 'a', kind), (2, 'a', kind),
                                                                   (3, 'b', 'created')])
        assert br._unmarked_event_marks(conn, ['a', 'b', 'missing']) == {
            'a': (2, 2), 'b': (0, None), 'missing': (0, None)}
    finally:
        conn.close()
