"""Observer access must not initialize, recreate, or wait minutes on a board."""
from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as connections
from hermes_cli import kanban_failed_result_observer as observer
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


def test_observer_connection_does_not_initialize(board, monkeypatch):
    _, context = board
    def forbidden(*args, **kwargs):
        pytest.fail('observer must not initialize or migrate an existing board')
    monkeypatch.setattr(connections, '_cross_process_init_lock', forbidden)
    with observer.connect_existing(context['db']) as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 250
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert conn.execute('SELECT id FROM tasks').fetchone()[0] == context['task_id']
        assert conn._dispatch_identity is not None


def test_observer_connection_never_creates_missing_board(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        with observer.connect_existing(path):
            pytest.fail('missing board opened')
    assert not path.exists()


def test_observer_connection_bounds_busy_wait(board):
    conn, context = board
    with kb.write_txn(conn):
        start = time.monotonic()
        with observer.connect_existing(context['db']) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        assert time.monotonic() - start < 2
