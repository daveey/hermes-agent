"""Real dependency lifecycle regressions: stopped scope never becomes parent success. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_pacing as pacing
from tests.hermes_cli import test_kanban_spend_budget as spend

board = pacing.board


def restrict(conn, tid):
    spend._runs(conn, tid, 2, usage={'cache_read_tokens': 1})
    assert budget.hold(conn, tid, lim=budget.Limits(1, 1)) == 'triage'
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'decision_hold_restricted', {'cause': 'scope_stopped'})


def test_archived_scope_stopped_parent_never_satisfies_child(board):
    parent = kb.create_task(board, title='Historical scope stop', assignee='default')
    child = kb.create_task(board, title='Waiter acceptance', assignee='default', parents=[parent])
    restrict(board, parent)
    assert kb.archive_task(board, parent)
    assert kb.get_task(board, parent).status == 'archived'
    assert kb.parent_states(board, child) == [(parent, 'triage')]
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == 'todo'
    assert kb.claim_task(board, child) is None
    assert board.execute('SELECT COUNT(*) FROM task_links WHERE parent_id=? AND child_id=?', (parent, child)).fetchone()[0] == 1


@pytest.mark.parametrize('stale_status', ['todo', 'blocked'])
def test_recompute_never_promotes_stopped_scope_from_stale_status(board, stale_status):
    tid = kb.create_task(board, title='Historical mismatched status', assignee='default')
    restrict(board, tid)
    # Simulate pre-fix legacy row, not a supported release or a mocked hold predicate.
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET status=? WHERE id=?', (stale_status, tid))
    assert kb.recompute_ready(board) == 0
    assert kb.get_task(board, tid).status == stale_status
    assert kb.claim_task(board, tid) is None


def test_ordinary_archived_and_all_done_parents_release_exactly_once(board):
    archived = kb.create_task(board, title='Ordinary withdrawn prerequisite', assignee='default')
    done = kb.create_task(board, title='Successful prerequisite', assignee='default')
    child = kb.create_task(board, title='Ordinary dependency waiter', assignee='default', parents=[archived, done])
    assert kb.archive_task(board, archived)
    assert kb.get_task(board, child).status == 'todo'
    assert kb.complete_task(board, done, summary='Fixture acceptance satisfied')
    assert kb.get_task(board, child).status == 'ready'
    count = board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='promoted'", (child,)).fetchone()[0]
    kb.recompute_ready(board)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='promoted'", (child,)).fetchone()[0] == count == 1


@pytest.mark.parametrize('incremental', [False, True])
def test_foreign_parent_mirrors_preserve_actual_archived_stop(board, monkeypatch, incremental):
    import sqlite3
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_parent_refresh as refresh
    from hermes_cli import kanban_move_parent_evidence as evidence
    parent = kb.create_task(board, title='Foreign stopped scope', assignee='default')
    ordinary = kb.create_task(board, title='Foreign ordinary archive', assignee='default')
    restrict(board, parent)
    assert kb.archive_task(board, parent)
    assert kb.archive_task(board, ordinary)
    live = sqlite3.connect(':memory:', isolation_level=None)
    live.row_factory = sqlite3.Row
    live.executescript('CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT);'
                       'CREATE TABLE task_links(parent_id TEXT,child_id TEXT);'
                       'CREATE TABLE kanban_sync_remote_parents(parent_id TEXT PRIMARY KEY,status TEXT,refreshed_at INTEGER);'
                       "INSERT INTO tasks VALUES('child','todo');")
    live.executemany("INSERT INTO task_links VALUES(?,'child')", [(parent,), (ordinary,)])
    # This fixture isolates status transport; unrelated move-result evidence is not under test.
    monkeypatch.setattr(evidence, 'prepare', lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(evidence, 'stored', lambda *a: {parent: None, ordinary: None})
    try:
        if incremental:
            refresh._refresh_rows(live, board, {parent, ordinary}, scoped={})
        else:
            sync.refresh_remote_parents(live, board, scoped={})
        assert dict(kb.parent_states(live, 'child', remote=True)) == {parent: 'triage', ordinary: 'archived'}
        # Exact source correlation matters even when the event table has an id column.
        unrelated = kb.create_task(board, title='Unrelated archived control', assignee='default')
        assert kb.archive_task(board, unrelated)
        from hermes_cli.kanban_containment_terminal import parent_status_sql
        expression = parent_status_sql(board)
        rows = dict(board.execute(f'SELECT id,{expression} FROM tasks WHERE id IN (?,?,?)', (parent, ordinary, unrelated)))
        assert rows == {parent: 'triage', ordinary: 'archived', unrelated: 'archived'}
    finally:
        live.close()
