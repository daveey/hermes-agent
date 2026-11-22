"""Public Kanban write entry points never issue REPLACE on tasks/task_runs (PR4045 review 6014073575).

Runtime proof in place of a static audit: every statement the board connection executes while
the public lifecycle runs is captured with ``set_trace_callback`` and checked by the same
preflight the factory guard uses.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_replace_guard import ProtectedRowReplaceError, ReplaceGuardMixin, reject_replace
from tests.hermes_cli import test_kanban_protect_stopped_local as support

board = support.board


def _replace_statements(statements):
    found = []
    for sql in statements:
        try:
            reject_replace(sql)
        except ProtectedRowReplaceError:
            found.append(sql)
    return found


def test_board_connection_carries_the_guard(board):
    assert isinstance(board, ReplaceGuardMixin)


def test_public_lifecycle_never_replaces_protected_rows(board, monkeypatch):
    statements = []
    board.set_trace_callback(statements.append)
    try:
        parent = kb.create_task(board, title='trace parent', assignee='default')
        child = kb.create_task(board, title='trace child', assignee='default')
        kb.link_tasks(board, parent, child)
        kb.add_comment(board, parent, 'default', 'trace comment')
        kb.recompute_ready(board)
        assert kb.claim_task(board, parent, claimer='default')
        kb.heartbeat_claim(board, parent, claimer='default')
        kb.block_task(board, parent, reason='trace block')
        kb.unblock_task(board, parent)
        kb.recompute_ready(board)
        assert kb.claim_task(board, parent, claimer='default')
        kb.complete_task(board, parent, result='trace done', force=True)
        kb.recompute_ready(board)
        kb.release_stale_claims(board)
        kb.archive_task(board, child)
        # Ended-run + protected-stop path (the reviewer's target rows).
        support.fixture(board, monkeypatch)
    finally:
        board.set_trace_callback(None)
    writes = [s for s in statements if s.lstrip().split(None, 1)[:1] and s.lstrip().split(None, 1)[0].upper() in ('INSERT', 'UPDATE', 'REPLACE', 'WITH')]
    assert any('task_runs' in s for s in writes), 'trace must cover run writes'
    assert any('tasks' in s for s in writes), 'trace must cover task writes'
    assert _replace_statements(statements) == []


def test_trace_detector_catches_replace(board):
    statements = []
    board.set_trace_callback(statements.append)
    try:
        with pytest.raises(ProtectedRowReplaceError):
            board.execute("INSERT OR REPLACE INTO tasks (id) VALUES ('x')")
        # sqlite3's own (unguarded) path would have traced it; the guard refuses before SQLite sees it.
        assert _replace_statements(statements) == []
        assert _replace_statements(["INSERT OR REPLACE INTO task_runs (id) VALUES (1)"])
    finally:
        board.set_trace_callback(None)
