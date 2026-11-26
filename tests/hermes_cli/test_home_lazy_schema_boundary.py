"""Writer/savepoint/rollback controls for dormant late-schema registration. [daveey]"""
import sqlite3

import pytest
from hermes_cli import kanban_db_connect as dbc
from tests.hermes_cli import test_home_lazy_schema_custody as lazy
from tests.hermes_cli import test_kanban_home_transfer_fence as support
from tests.hermes_cli import test_kanban_home_source_stop as stop

board = support.board


def test_nested_late_insert_cannot_escape_outer_commit(board):
    task = support.seed(board)
    with dbc.write_txn(board):
        support.prepare(board, task)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            with dbc.write_txn(board, allow_nested=True):
                lazy.late(board)
                board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('nested', task, 'late'))
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='late_custody'").fetchone()
    assert not board.in_transaction


def test_nested_schema_rollback_does_not_leave_speculative_coverage(board):
    task = support.seed(board)
    with dbc.write_txn(board):
        support.prepare(board, task)
        with pytest.raises(ValueError, match='fixture rollback'):
            with dbc.write_txn(board, allow_nested=True):
                lazy.late(board)
                raise ValueError('fixture rollback')
    # Reusing the rolled-back schema cookie for a different table is not a cache hit.
    with dbc.write_txn(board):
        lazy.late(board, 'other_custody')
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute('INSERT INTO other_custody VALUES (?,?,?)', ('protected', task, 'late'))


def test_rolled_back_first_install_does_not_poison_next_schema(board):
    task = support.seed(board)
    with pytest.raises(ValueError, match='fixture rollback'):
        with dbc.write_txn(board):
            support.prepare(board, task)
            lazy.late(board)
            raise ValueError('fixture rollback')
    with dbc.write_txn(board):
        support.prepare(board, task)
        lazy.late(board, 'other_custody')
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute('INSERT INTO other_custody VALUES (?,?,?)', ('protected', task, 'late'))


@pytest.mark.parametrize('settle', ['abort', 'finalize'])
def test_exact_settlement_allows_new_custody_table(board, settle):
    from hermes_cli import kanban_home_transfer as transfer
    task = support.seed(board)
    record = stop.record(board, task)
    with dbc.write_txn(board):
        support.prepare(board, task, nonce=record['nonce'])
    with dbc.write_txn(board):
        if settle == 'abort':
            transfer.abort_source(board, {**record, 'phase': 'aborted', 'rows': []})
        else:
            transfer.finalize_source(board, record)
        lazy.late(board)
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('settled', task, 'allowed'))
    assert board.execute('SELECT value FROM late_custody').fetchone()[0] == 'allowed'


def test_healthy_new_table_has_replace_and_update_sql_guards(board):
    task = support.seed(board)
    with dbc.write_txn(board):
        support.prepare(board, task)
        lazy.late(board)
    # No native wrapper is needed after registration: the durable SQL fence acts.
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'late'))
    with dbc.write_txn(board):
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('healthy', 'other-task', 'original'))
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        board.execute('INSERT OR REPLACE INTO late_custody VALUES (?,?,?)', ('healthy', task, 'late'))
    assert tuple(board.execute('SELECT * FROM late_custody').fetchone()) == ('healthy', 'other-task', 'original')


def test_noop_writer_does_not_reinstall_schema_or_scan_custody(board, monkeypatch):
    from hermes_cli import kanban_home_schema as schema
    task = support.seed(board)
    with dbc.write_txn(board):
        support.prepare(board, task)
    version = board.execute('PRAGMA schema_version').fetchone()[0]
    def unexpected(*args):
        raise AssertionError('unchanged committed schema must not be rediscovered')
    monkeypatch.setattr(schema, '_tables', unexpected)
    with dbc.write_txn(board):
        board.execute('SELECT 1')
    assert board.execute('PRAGMA schema_version').fetchone()[0] == version


@pytest.mark.parametrize('action', ['update', 'delete'])
def test_removed_existing_custody_trigger_is_not_silently_adopted(board, action):
    task = support.seed(board)
    with dbc.write_txn(board):
        lazy.late(board)
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'original'))
        support.prepare(board, task)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute(f'DROP TRIGGER home_prepared1_late_custody_{action}')
            if action == 'delete':
                board.execute('DELETE FROM late_custody')
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        board.execute("UPDATE late_custody SET value='late'")
    assert tuple(board.execute('SELECT * FROM late_custody').fetchone()) == ('protected', task, 'original')
