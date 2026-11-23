"""Real dependency lifecycle regressions: stopped scope never becomes parent success. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_containment_actor as support
from tests.hermes_cli import test_kanban_spend_budget as spend

board = support.board


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
    assert kb.unsatisfied_parents(board, child) == [(parent, 'archived:restricted')]
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
        assert dict(kb.parent_states(live, 'child', remote=True)) == {parent: 'archived:restricted', ordinary: 'archived'}
        # Exact source correlation: an unrelated ordinary archive never inherits another card's stop.
        unrelated = kb.create_task(board, title='Unrelated archived control', assignee='default')
        assert kb.archive_task(board, unrelated)
        assert kb.parent_blocks(board, parent, 'archived') is True
        assert kb.parent_blocks(board, ordinary, 'archived') is False
        assert kb.parent_blocks(board, unrelated, 'archived') is False
    finally:
        live.close()


# Review on eeb369b4 (P1): a validly renewed, promoted and completed scope must release its children (now via
# main's parent_blocks gate, which labels restricted terminal parents '<status>:restricted').
from tests.hermes_cli import test_kanban_scope_renewal as scope


def renewed_and_done(board, monkeypatch):
    parent, _, episode = scope.stopped(board, monkeypatch)
    scope.link(board, parent)
    comment = scope.mandate(board, parent)
    scope.renew(board, parent, episode, comment)
    child = kb.create_task(board, title='Waits on the renewed scope', assignee='default', parents=[parent])
    promoted, refusal = kb.promote_task(board, parent, actor='fixture', reason='renewed scope')
    assert promoted is True, refusal
    assert kb.complete_task(board, parent, summary='Renewed scope delivered')
    return parent, child, episode, comment


def test_renewed_promoted_completed_scope_releases_its_child(board, monkeypatch):
    parent, child, _, _ = renewed_and_done(board, monkeypatch)
    assert kb.get_task(board, parent).status == 'done'
    assert kb.parent_states(board, child) == [(parent, 'done')]
    assert kb.unsatisfied_parents(board, child) == []
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == 'ready'


def test_edited_mandate_new_stop_or_safety_keep_the_child_waiting(board, monkeypatch):
    for spoil in ('edit', 'stop', 'safety'):
        parent, child, episode, comment = renewed_and_done(board, monkeypatch)
        with kb.write_txn(board):
            if spoil == 'edit':
                board.execute("UPDATE task_comments SET body='Do everything' WHERE id=?", (comment,))
            else:
                cause = 'scope_stopped' if spoil == 'stop' else 'safety_refusal'
                kb._append_event(board, parent, 'decision_hold_restricted', {'cause': cause, 'episode': episode})
        assert kb.unsatisfied_parents(board, child) == [(parent, 'done:restricted')], spoil


@pytest.mark.parametrize('incremental', [False, True])
def test_foreign_mirrors_release_a_renewed_completed_parent(board, monkeypatch, incremental):
    import sqlite3
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_parent_refresh as refresh
    from hermes_cli import kanban_move_parent_evidence as evidence
    parent, _, _, _ = renewed_and_done(board, monkeypatch)
    stopped = kb.create_task(board, title='Unrenewed stop control', assignee='default')
    restrict(board, stopped)
    assert kb.archive_task(board, stopped)
    live = sqlite3.connect(':memory:', isolation_level=None)
    live.row_factory = sqlite3.Row
    live.executescript('CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT);'
                       'CREATE TABLE task_links(parent_id TEXT,child_id TEXT);'
                       'CREATE TABLE kanban_sync_remote_parents(parent_id TEXT PRIMARY KEY,status TEXT,refreshed_at INTEGER);'
                       "INSERT INTO tasks VALUES('child','todo');")
    live.executemany("INSERT INTO task_links VALUES(?,'child')", [(parent,), (stopped,)])
    monkeypatch.setattr(evidence, 'prepare', lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(evidence, 'stored', lambda *a: {parent: None, stopped: None})
    try:
        if incremental:
            refresh._refresh_rows(live, board, {parent, stopped}, scoped={})
        else:
            sync.refresh_remote_parents(live, board, scoped={})
        assert dict(kb.parent_states(live, 'child', remote=True)) == {parent: 'done', stopped: 'archived:restricted'}
    finally:
        live.close()
