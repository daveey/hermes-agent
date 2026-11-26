"""First native preparation must retain its installed identity baseline. [daveey]"""
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as dbc, kanban_home_schema as schema
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import test_home_lazy_schema_custody as lazy
from tests.hermes_cli import test_kanban_home_transfer_fence as support

board = support.board


def custody(conn, task):
    with dbc.write_txn(conn):
        lazy.late(conn)
        conn.execute("INSERT INTO late_custody VALUES ('protected',?,'shared')", (task,))


def rows(conn):
    return [tuple(r) for r in conn.execute('SELECT * FROM late_custody')]


def replace(conn):
    conn.execute("INSERT OR REPLACE INTO late_custody VALUES ('healthy','other-task','shared')")


def test_authentic_first_prepare_identity_change_cannot_erase_custody(board):
    task = support.seed(board)
    custody(board, task)
    before = rows(board)
    # Keep the reviewer's expected-invariant oracle: either atomic refusal or
    # correct replacement coverage must preserve the protected original row.
    try:
        with dbc.write_txn(board):
            support.prepare(board, task)
            board.execute('CREATE UNIQUE INDEX late_value ON late_custody(value)')
    except sqlite3.IntegrityError:
        assert rows(board) == before
        return
    try:
        with dbc.write_txn(board):
            replace(board)
    except sqlite3.IntegrityError:
        pass
    assert rows(board) == before


@pytest.mark.parametrize(('change', 'repeat'), [
    ('index', False), ('index', True), ('drop', False), ('drop', True),
    ('alter', False), ('alter', True), ('trigger', False),
])
def test_first_prepare_rejects_changed_guarded_schema(board, change, repeat):
    task = support.seed(board)
    custody(board, task)
    before = rows(board)
    sql = {
        'index': 'CREATE UNIQUE INDEX late_value ON late_custody(value)',
        'drop': 'DROP TABLE late_custody',
        'alter': 'ALTER TABLE late_custody ADD COLUMN extra TEXT',
        'trigger': 'DROP TRIGGER home_prepared1_late_custody_delete',
    }[change]
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared: guarded'):
        with dbc.write_txn(board):
            support.prepare(board, task)
            board.execute(sql)
            if repeat:
                # A second prepare must not rebaseline already installed guards.
                support.prepare(board, task)
    assert rows(board) == before
    assert not transfer.frozen(board, task)
    assert schema._FIRST_INSTALL.get() is None


def test_first_prepare_then_index_and_replace_in_same_writer_rolls_back(board):
    task = support.seed(board)
    custody(board, task)
    before = rows(board)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        with dbc.write_txn(board):
            support.prepare(board, task)
            board.execute('CREATE UNIQUE INDEX late_value ON late_custody(value)')
            replace(board)
    assert rows(board) == before
    assert not transfer.frozen(board, task)


@pytest.mark.parametrize('installed', [False, True])
def test_index_before_prepare_gets_durable_replace_coverage(board, installed):
    task = support.seed(board)
    custody(board, task)
    if installed:
        other = support.seed(board)
        with dbc.write_txn(board):
            support.prepare(board, other, nonce='transfer-other-0001')
        with pytest.raises(sqlite3.IntegrityError, match='guarded identity changed'):
            with dbc.write_txn(board):
                support.prepare(board, task)
                board.execute('CREATE UNIQUE INDEX late_value ON late_custody(value)')
        assert not transfer.frozen(board, task)
        return
    with dbc.write_txn(board):
        board.execute('CREATE UNIQUE INDEX late_value ON late_custody(value)')
        support.prepare(board, task)
    before = rows(board)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        replace(board)  # Durable raw-SQL guard, no native wrapper needed.
    assert rows(board) == before


def test_savepoint_rollback_then_fresh_install_replaces_speculative_snapshot(board):
    task = support.seed(board)
    custody(board, task)
    with dbc.write_txn(board):
        with pytest.raises(ValueError, match='rollback'):
            with dbc.write_txn(board, allow_nested=True):
                support.prepare(board, task)
                raise ValueError('rollback')
        board.execute('CREATE UNIQUE INDEX late_value ON late_custody(value)')
        support.prepare(board, task)
    before = rows(board)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        replace(board)
    assert rows(board) == before
    assert schema._FIRST_INSTALL.get() is None


def test_rolled_back_only_install_leaves_no_context_or_committed_cache(board):
    task = support.seed(board)
    with dbc.write_txn(board):
        with pytest.raises(ValueError, match='rollback'):
            with dbc.write_txn(board, allow_nested=True):
                support.prepare(board, task)
                raise ValueError('rollback')
    assert not transfer.frozen(board, task)
    assert board._home_schema_committed is None
    assert schema._FIRST_INSTALL.get() is None
    with dbc.write_txn(board):
        support.prepare(board, task)
        lazy.late(board)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        board.execute('INSERT INTO late_custody VALUES (?,?,?)', ('protected', task, 'late'))
