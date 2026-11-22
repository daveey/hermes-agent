"""Original review6014073575 probes through the canonical board factory."""
import sqlite3

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_protect_stopped as protect
from hermes_cli import kanban_lockguard
from tests.hermes_cli import test_kanban_protect_stopped_local as support

board = support.board


@pytest.mark.parametrize('table', ['tasks', 'task_runs'])
def test_review_protected_row_replace_probe(board, monkeypatch, table):
    _, _, session, target, old, args = support.fixture(board, monkeypatch)
    with scoped_current_session_id(session):
        assert protect.protect(board, board='default', **args)['ok']
    board.execute('PRAGMA recursive_triggers=OFF')
    assert board.execute('PRAGMA recursive_triggers').fetchone()[0] == 0
    identity = target if table == 'tasks' else old
    original = dict(board.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone())
    replacement = {**original, 'status': 'ready' if table == 'tasks' else 'done'}
    if table == 'task_runs':
        replacement['outcome'] = 'completed'
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute(f'UPDATE {table} SET status=? WHERE id=?', (replacement['status'], identity))
    columns = ','.join('"' + key + '"' for key in replacement)
    placeholders = ','.join('?' for _ in replacement)
    error = getattr(kanban_lockguard, 'ProtectedRowReplaceError', sqlite3.IntegrityError)
    with pytest.raises(error, match=f'REPLACE.*{table}'):
        board.execute(f'INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})', tuple(replacement.values()))
    assert dict(board.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()) == original
    assert kb.get_task(board, target).status == 'triage'
