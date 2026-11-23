"""Independent old-writer DB refusal; not an external-process drain receipt."""
import multiprocessing
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_writer_gate as gate
from tests.hermes_cli import test_kanban_sync_writer_gate as helpers

board = helpers.board


def _writer(path, revision, channel):
    conn = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- old-writer fixture
    try:
        if revision is not None:
            conn.create_function('replay_writer_revision', 0, lambda: revision)
        conn.execute('SELECT count(*) FROM tasks').fetchone()
        channel.send('preopened')
        assert channel.recv() == 'write'
        try:
            conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_child','child','todo',1)")
        except sqlite3.Error as exc:
            channel.send(('refused', str(exc)))
        else:
            channel.send(('written', None))
    finally:
        conn.close()
        channel.close()


@pytest.mark.parametrize('revision', [None, 0, 1])
def test_preopened_independent_writer_checks_persisted_minimum(board, revision):
    path, conn = board
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_writer, args=(path, revision, child))
    proc.start()
    child.close()
    try:
        assert parent.poll(30), 'child did not open its independent SQLite connection'
        assert parent.recv() == 'preopened'
        with kbc.write_txn(conn):
            gate.activate(conn)  # Disposable fixture only, never a deployment.
        before = list(conn.iterdump())
        parent.send('write')
        assert parent.poll(30), 'child did not report its write outcome'
        outcome, detail = parent.recv()
        proc.join(10)
        assert proc.exitcode == 0
        if revision == 1:
            assert outcome == 'written'
            assert conn.execute("SELECT title FROM tasks WHERE id='t_child'").fetchone()[0] == 'child'
        else:
            assert outcome == 'refused'
            assert ('replay_writer_revision' if revision is None else 'revision too old') in detail
            assert list(conn.iterdump()) == before
        assert conn.execute('SELECT minimum FROM replay_writer_gate1').fetchone()[0] == 1
    finally:
        parent.close()
        if proc.is_alive():
            proc.terminate()
        proc.join(10)
