"""Late task-table custody at the native committed writer boundary. [daveey]"""
from pathlib import Path
import sqlite3

import pytest
from hermes_cli import kanban_db_connect as dbc, kanban_home_transfer as transfer
from tests.hermes_cli import test_kanban_home_transfer_fence as support

board = support.board


def late(conn, table='late_custody'):
    conn.execute(f'CREATE TABLE {table} (episode TEXT PRIMARY KEY, task_id TEXT NOT NULL, value TEXT)')


@pytest.mark.parametrize('same_writer', [False, True])
def test_late_table_protected_insert_rolls_back(board, same_writer):
    task = support.seed(board)
    with dbc.write_txn(board):
        support.prepare(board, task)
    if not same_writer:
        with dbc.write_txn(board):
            late(board)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            if same_writer:
                late(board)
            board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'late'))
    assert transfer.frozen(board, task)
    if same_writer:
        assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='late_custody'").fetchone()
    else:
        assert board.execute('SELECT * FROM late_custody').fetchall() == []


def test_prepare_and_late_ddl_same_transaction_are_atomic(board):
    task = support.seed(board)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            support.prepare(board, task)
            late(board)
            board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'late'))
    assert not transfer.frozen(board, task)
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='late_custody'").fetchone()


def test_healthy_lazy_table_commits_then_reopen_has_full_sql_guards(board):
    task = support.seed(board)
    with dbc.write_txn(board):
        support.prepare(board, task)
        late(board)
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('healthy', 'other-task', 'original'))
    path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    reopened = dbc.connect(path)
    try:
        for sql, args in [
            ('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'late')),
            ('UPDATE late_custody SET task_id=?', (task,)),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
                with dbc.write_txn(reopened):
                    reopened.execute(sql, args)
        assert tuple(reopened.execute('SELECT * FROM late_custody').fetchone()) == ('healthy', 'other-task', 'original')
    finally:
        reopened.close()


def test_real_lazy_notice_writer_cannot_commit_prepared_custody(board):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_unproven import note_check
    task = support.seed(board)
    with dbc.write_txn(board):
        kb._append_event(board, task, 'scheduled', {'wake_at': 9999999999, 'wake_check': 'probe', 'probe_host': 'worker-a2'})
        board.execute("UPDATE tasks SET status='scheduled',sync_version=7 WHERE id=?", (task,))
        board.execute('DELETE FROM kanban_sync_outbox')
        support.prepare(board, task)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            note_check(board, task, 'probe', '', 100)


@pytest.mark.parametrize('change', ['drop', 'alter', 'index'])
def test_existing_guarded_identity_cannot_change_while_prepared(board, change):
    task = support.seed(board)
    with dbc.write_txn(board):
        late(board)
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'original'))
        support.prepare(board, task)
    sql = {
        'drop': 'DROP TABLE late_custody',
        'alter': 'ALTER TABLE late_custody ADD COLUMN extra TEXT',
        'index': 'CREATE UNIQUE INDEX late_value ON late_custody(value)',
    }[change]
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute(sql)
    assert tuple(board.execute('SELECT * FROM late_custody').fetchone()) == ('protected', task, 'original')


def test_existing_custody_before_first_prepare_remains_valid(board):
    task = support.seed(board)
    with dbc.write_txn(board):
        late(board)
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'original'))
        support.prepare(board, task)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            board.execute('DELETE FROM late_custody')
    assert tuple(board.execute('SELECT * FROM late_custody').fetchone()) == ('protected', task, 'original')
