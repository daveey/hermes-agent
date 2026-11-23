"""Migrated-core differential and authority gates for the owned snapshot seam."""
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_session_acquisition as acquisition
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_session_activity_card_details as details
from tests.hermes_cli import test_kanban_execution_receipt as receipt_fixtures

board = receipt_fixtures.board
NOW = 2_000_000_000


def _path(conn):
    from pathlib import Path
    return Path(next(row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'))


def _compare(conn, monkeypatch, statuses, ids=('session',)):
    path = _path(conn)
    monkeypatch.setattr(activity.time, 'time', lambda: NOW)
    monkeypatch.setattr(details, '_replica_for', lambda *a: None)
    legacy = {row['id']: row for row in activity._read_session_tasks(path, list(ids), statuses)}
    with closing(activity._open_ro(path, timeout=0.5)) as captured:
        captured.row_factory = sqlite3.Row
        captured.execute('BEGIN')
        statements = []
        captured.set_trace_callback(statements.append)
        owned, pairs = acquisition.read_session_task_set(captured, path, ids, statuses, observed_at=NOW,
                                                         scope=observations.card_scope(path))
        assert captured.in_transaction
        assert not any(sql.lstrip().upper().startswith(('CREATE ', 'ALTER ', 'INSERT ', 'UPDATE ', 'DELETE ',
                                                      'COMMIT', 'ROLLBACK')) for sql in statements)
    assert owned == legacy
    return owned, pairs


@pytest.mark.parametrize('mode', ['local', 'primary', 'unavailable'])
@pytest.mark.parametrize('disposition', ['delivered', 'rejected', None])
def test_migrated_done_authority_and_receipt_differential(board, monkeypatch, mode, disposition):
    tid = receipt_fixtures.finish(board, receipt_fixtures.card(board), disposition)
    if mode != 'local':
        board.execute('CREATE TABLE IF NOT EXISTS kanban_sync_state (mode TEXT)')
        board.execute('DELETE FROM kanban_sync_state')
        board.execute('INSERT INTO kanban_sync_state(mode) VALUES (?)',
                      ('primary' if mode == 'primary' else 'unavailable',))
    rows, pairs = _compare(board, monkeypatch, ('done',))
    card = rows[tid]
    assert pairs == {('session', tid): 'created'}
    assert card['_execution_scope'] == {'gateway': receipt_fixtures.GATEWAY, 'profile': 'default', 'board': 'default'}
    assert ('_execution_done' in card) == (mode == 'local' and disposition == 'delivered')
    assert ('_execution_source' in card) == (mode == 'local')
    assert card.get('_execution_error', False) == (mode != 'local')
    if '_execution_done' in card:
        assert card['_execution_done']['delivery_receipt']['verified'] is True


@pytest.mark.parametrize('mode', ['local', 'primary', 'unavailable'])
def test_current_run_source_scope_and_primary_owner_evidence(board, monkeypatch, mode):
    tid = receipt_fixtures.card(board)
    assert kb.claim_task(board, tid)
    rid = kb.get_task(board, tid).current_run_id
    for table in ('task_runs', 'task_events'):
        if 'uid' not in {r[1] for r in board.execute(f'PRAGMA table_info({table})')}:
            board.execute(f'ALTER TABLE {table} ADD COLUMN uid TEXT')
    board.execute('UPDATE task_runs SET uid=?,last_heartbeat_at=? WHERE id=?', ('run-uid', NOW - 10, rid))
    board.execute('INSERT INTO task_events(task_id,run_id,kind,created_at,payload,uid) VALUES (?,?,?,?,?,?)',
                  (tid, rid, 'heartbeat', NOW - 10, '{}', 'beat-uid'))
    if mode != 'local':
        board.execute('CREATE TABLE IF NOT EXISTS kanban_sync_state (mode TEXT)')
        board.execute('DELETE FROM kanban_sync_state')
        board.execute('INSERT INTO kanban_sync_state(mode) VALUES (?)',
                      ('primary' if mode == 'primary' else 'unavailable',))
    rows, _ = _compare(board, monkeypatch, ('running',))
    card = rows[tid]
    if mode == 'primary':
        assert card['_replica_run']['run_id'] == 'run-uid'
        assert '_execution_source' not in card and '_execution_error' not in card
    elif mode == 'local':
        assert card['_execution_source']['observed_at'] == NOW
        assert card['current_run_started_at'] == kb.get_task(board, tid).started_at
    else:
        assert card['_execution_error'] is True and '_replica_run' not in card


@pytest.mark.parametrize('producer', ['clock', 'ci', 'deploy'])
def test_explicit_producer_wait_differential(board, monkeypatch, producer):
    tid = receipt_fixtures.card(board)
    payload = {'wake_at': NOW + 60, 'clock_only': True} if producer == 'clock' else {
        'awaiting': {'kind': producer, 'ref': 'fixture-ref', 'why': 'fixture wait'}, 'wake_at': NOW + 60}
    board.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    kb._append_event(board, tid, 'scheduled', payload)
    rows, _ = _compare(board, monkeypatch, ('scheduled',))
    assert rows[tid]['wait']['waiting_on']
    assert rows[tid]['wait'] == activity._local_wait_info(board, [tid], now=NOW)[tid]


@pytest.mark.parametrize('failure', ['source', 'wait'])
def test_optional_failures_preserve_rows_and_scope(board, monkeypatch, failure):
    from hermes_cli import kanban_execution_source
    tid = receipt_fixtures.card(board)

    def refused(*args, **kwargs):
        raise sqlite3.OperationalError('controlled optional read failure')

    if failure == 'source':
        monkeypatch.setattr(kanban_execution_source, 'assemble', refused)
    else:
        monkeypatch.setattr(activity, '_local_wait_info', refused)
    rows, _ = _compare(board, monkeypatch, ('ready',))
    assert tid in rows and rows[tid]['_execution_scope']['gateway'] == receipt_fixtures.GATEWAY
    assert rows[tid].get('_execution_error', False) == (failure == 'source')


def test_migrated_change_cursor_and_rows_share_one_physical_snapshot(board, monkeypatch):
    import sys
    from hermes_cli import kanban_task_change as changes

    tid = receipt_fixtures.card(board)
    path = _path(board)
    scope = observations.card_scope(path)
    opened = []
    auditing = [True]

    def record(event, args):
        if auditing[0] and event == 'sqlite3.connect':
            opened.append(args[0])

    sys.addaudithook(record)
    try:
        with closing(activity._open_ro(path, timeout=0.5)) as captured:
            assert len(opened) == 1
            captured.row_factory = sqlite3.Row
            captured.execute('BEGIN')
            _, cursor = changes.changed_since(captured, None)
            assert captured.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'Bounded work'
            board.execute("UPDATE tasks SET title='committed after cursor' WHERE id=?", (tid,))
            # Every underlying SQLite constructor emits the audit event, including
            # imported aliases: no monkeypatched connect facade can mask an open.
            cards, _ = acquisition.read_session_task_set(captured, path, ['session'], ('ready',),
                                                         observed_at=NOW, scope=scope)
            assert cards[tid]['title'] == 'Bounded work'
            assert changes.changed_since(captured, None)[1] == cursor
            assert captured.in_transaction and len(opened) == 1
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                captured.execute("UPDATE tasks SET title='forbidden' WHERE id=?", (tid,))
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            captured.execute('SELECT 1')
    finally:
        auditing[0] = False
    assert board.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'committed after cursor'


def test_named_board_profile_scope_comes_from_path_not_observing_profile(board, monkeypatch, tmp_path):
    tid = receipt_fixtures.card(board)
    monkeypatch.setattr(observations, 'local_scope', lambda: ('observer', 'unrelated'))
    path = tmp_path / 'profiles' / 'owner' / 'kanban' / 'boards' / 'work' / 'kanban.db'
    # card_scope consumes metadata only: no DB exists at this path.
    rows, _ = acquisition.read_session_task_set(board, path, ['session'], ('ready',), observed_at=NOW,
                                                 scope=observations.card_scope(path))
    assert rows[tid]['_execution_scope'] == {
        'gateway': 'observer', 'profile': 'owner', 'board': 'work'}
    assert rows[tid]['board'] == 'work'
