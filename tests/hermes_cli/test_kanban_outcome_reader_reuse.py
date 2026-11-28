"""Outcome evidence reuses guarded waiting readers, never sidebar custody or answers."""
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_session_outcome_tasks as lean
from hermes_cli import kanban_waiting_reader as waiting
from tests.hermes_cli.test_kanban_session_outcome_tasks import _connect, _insert_task


@pytest.fixture(autouse=True)
def readers():
    waiting.close_all()
    yield
    waiting.close_all()


def test_warm_outcome_retains_reader_not_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    writer = _connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    _insert_task(writer, 't_live', 'running', 'chat')
    writer.commit()
    opened = []
    original = activity._open_ro

    def record(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(activity, '_open_ro', record)
    try:
        for status in ('running', 'blocked', 'review'):
            writer.execute('UPDATE tasks SET status=?', (status,))
            writer.commit()
            rows = lean._read_lean_session_tasks(path, ['chat'], (status,))
            assert [row['status'] for row in rows] == [status]
        assert len(opened) == 1, 'repeated outcome evidence reopened guarded SQLite'
        assert not opened[0].in_transaction
        with waiting.reading(path) as borrowed:
            assert borrowed is opened[0]
            assert borrowed.row_factory is None
    finally:
        waiting.close_all()
        writer.close()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


def test_outcome_exception_does_not_pin_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    writer = _connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.commit()
    original = lean.select_listed_local_tasks

    def fail(conn, *args):
        conn.execute('SELECT * FROM tasks').fetchall()
        raise ValueError('selection failed')

    monkeypatch.setattr(lean, 'select_listed_local_tasks', fail)
    try:
        with pytest.raises(ValueError, match='selection failed'):
            lean._read_lean_session_tasks(path, ['chat'], ('running',))
        assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        monkeypatch.setattr(lean, 'select_listed_local_tasks', original)
        _insert_task(writer, 't_new', 'running', 'chat')
        writer.commit()
        assert [row['id'] for row in lean._read_lean_session_tasks(path, ['chat'], ('running',))] == ['t_new']
    finally:
        writer.close()
