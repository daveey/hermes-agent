"""Core mirrors Board parentsSatisfied, including terminal non-success parents."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_worker_node as node
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def local(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, False)
    conn = kbc.connect(home / 'kanban.db')
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize('status,cause,contained,renewed,blocked', [
    ('archived', 'safety_refusal', False, False, True),
    ('done', 'safety_refusal', False, False, True),
    ('archived', 'scope_stopped', False, False, True),
    ('done', 'scope_stopped', False, False, True),
    ('done', 'scope_stopped', False, True, False),
    ('archived', None, True, False, True),
    ('done', None, False, False, False),
    ('archived', None, False, False, False),
])
def test_terminal_parent_gates_every_local_surface(local, status, cause, contained, renewed, blocked):
    parent = kb.create_task(local, title='parent', assignee='worker')
    child = kb.create_task(local, title='child', assignee='worker', parents=[parent])
    with kb.write_txn(local):
        if cause:
            kb._append_event(local, parent, 'spend_budget_triaged', {'cause': cause})
        local.execute('UPDATE tasks SET status=?,containment_episode=? WHERE id=?',
                      (status, 'fixture-episode' if contained else None, parent))
    if renewed:
        event = dict(local.execute("SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'",
                                   (parent,)).fetchone())
        comment = kb.add_comment(local, parent, 'default', 'Renewed scope: finish parent.', writer_kind='human')
        row = dict(local.execute('SELECT * FROM task_comments WHERE id=?', (comment,)).fetchone())
        with kb.write_txn(local):
            kb._append_event(local, parent, 'scope_renewed', {
                'version': 1, 'episode': renewal.key(event), 'mandate_comment_uid': renewal.comment_ref(row),
                'mandate_body_sha256': renewal.digest(row['body']),
                'covered_restriction_uids': [renewal.key(event)]})
        assert not decision.execution_restricted(local, parent)
    assert kb.parent_blocks(local, parent, status) is blocked
    assert kb._parents_satisfied(local, child) is not blocked
    waiting = kb.unsatisfied_parents(local, child)
    if cause and blocked:
        assert waiting == [(parent, status + ':restricted')]
    else:
        assert bool(waiting) is blocked
    for _ in range(3):
        kb.recompute_ready(local)
    assert kb.get_task(local, child).status == ('todo' if blocked else 'ready')
    assert local.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='promoted'",
                         (child,)).fetchone()[0] == (0 if blocked else 1)
    late_child = kb.create_task(local, title='newly linked child', assignee='worker')
    kb.link_tasks(local, parent, late_child)
    assert kb.get_task(local, late_child).status == ('todo' if blocked else 'ready')


@pytest.mark.parametrize('cause,contained', [('safety_refusal', False), ('scope_stopped', False), (None, True), (None, False)])
def test_worker_candidates_gate_terminal_parent(local, cause, contained):
    # The whole-board replica includes transport-only task columns.
    for column, kind in [('created_on', 'TEXT'), ('lease_holder', 'TEXT'), ('lease_expires', 'INTEGER')]:
        if column not in {r[1] for r in local.execute('PRAGMA table_info(tasks)')}:
            local.execute(f'ALTER TABLE tasks ADD COLUMN {column} {kind}')
    parent = kb.create_task(local, title='parent', assignee='worker')
    child = kb.create_task(local, title='child', assignee='worker', parents=[parent])
    with kb.write_txn(local):
        if cause:
            kb._append_event(local, parent, 'spend_budget_triaged', {'cause': cause})
        local.execute("UPDATE tasks SET status='archived',containment_episode=? WHERE id=?",
                      ('fixture-episode' if contained else None, parent))
        local.execute("UPDATE tasks SET status='ready',created_on='hub' WHERE id=?", (child,))
    assert (child in [r['id'] for r in node.candidates(local, 'worker')]) is (not cause and not contained)


@pytest.mark.parametrize('incremental', [False, True])
def test_foreign_restriction_changes_mirror_without_parent_status_change(local, tmp_path, incremental):
    from hermes_cli.kanban_sync_parent_refresh import _refresh_rows
    replica = kbc.connect(tmp_path / 'replica.db')
    try:
        parent = kb.create_task(replica, title='foreign parent', assignee='worker')
        child = kb.create_task(local, title='child', assignee='worker')
        with kb.write_txn(local):
            local.execute('CREATE TABLE kanban_sync_remote_parents (parent_id TEXT PRIMARY KEY, status TEXT, refreshed_at INTEGER)')
            local.execute('INSERT INTO task_links (parent_id,child_id) VALUES (?,?)', (parent, child))
            local.execute("UPDATE tasks SET status='todo' WHERE id=?", (child,))
        with kb.write_txn(replica):
            replica.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        refresh = (lambda: _refresh_rows(local, replica, {parent}, frozenset())) if incremental else (
            lambda: sync.refresh_remote_parents(local, replica))
        assert refresh() == [parent]
        assert kb._parents_satisfied(local, child)
        with kb.write_txn(replica):
            kb._append_event(replica, parent, 'spend_budget_triaged', {'cause': 'scope_stopped'})
        assert refresh() == [parent]
        assert kb.get_task(local, parent) is None
        assert kb.unsatisfied_parents(local, child) == [(parent, 'done:restricted')]
        assert not kb._parents_satisfied(local, child)
        assert kb.recompute_ready(local) == 0
        assert refresh() == []
    finally:
        replica.close()
