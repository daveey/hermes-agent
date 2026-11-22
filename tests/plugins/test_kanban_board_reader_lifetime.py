"""The incremental observer must not retain history-reader snapshots."""
import sqlite3

import pytest

from hermes_cli import kanban_generation
from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard import board_read
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


@pytest.mark.parametrize('fail', [False, True])
def test_reader_closed_before_projection_and_after_failure(board, monkeypatch, fail):
    helpers._seed()
    readers = []
    original_open = kanban_generation.open_ro

    def open_reader(*args, **kwargs):
        conn = original_open(*args, **kwargs)
        if kwargs.get('check_same_thread') is not False:  # retained commit observer is separate
            readers.append(conn)
        return conn

    monkeypatch.setattr(kanban_generation, 'open_ro', open_reader)
    original_materialize = board_read.materialize
    original_project = board_read.project

    def assert_closed():
        assert readers
        for conn in readers:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                conn.execute('SELECT 1')

    def project(*args):
        assert_closed()
        return original_project(*args)

    monkeypatch.setattr(board_read, 'project', project)
    if fail:
        def error(*args, **kwargs):
            raise sqlite3.OperationalError('injected materialization failure')
        monkeypatch.setattr(board_read, 'materialize', error)
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            helpers._read(board)
        assert_closed()
        monkeypatch.setattr(board_read, 'materialize', original_materialize)
    first = helpers._stable(helpers._read(board))
    count = len(readers)
    assert first == helpers._stable(helpers._read(board))
    assert len(readers) == count, 'unchanged commit reuses rows without opening a reader'


def test_commit_between_clock_sample_and_reader_open_is_not_lost(board, monkeypatch):
    ids, _ = helpers._seed()
    helpers._read(board)
    conn = kbc.connect()
    conn.execute("UPDATE tasks SET title = 'first' WHERE id = ?", (ids[0],))
    conn.commit()
    conn.close()
    original_open = kanban_generation.open_ro
    opened = []

    def interleaved_open(*args, **kwargs):
        if kwargs.get('check_same_thread') is not False:
            if not opened:
                writer = kbc.connect()
                writer.execute("UPDATE tasks SET title = 'second' WHERE id = ?", (ids[0],))
                writer.commit()
                writer.close()
            opened.append(True)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(kanban_generation, 'open_ro', interleaved_open)
    first = helpers._stable(helpers._read(board))
    second = helpers._stable(helpers._read(board))
    assert first == second
    assert len(opened) == 2, 'the interleaved commit must invalidate the older sampled key'
    assert any(t['title'] == 'second' for c in second['columns'] for t in c['tasks'])
