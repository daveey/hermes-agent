"""Actual dispatcher reclaim publishes own hold notices in its first tick."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home


def test_reclaim_tick_publishes_own_hold_without_releasing(kanban_home):
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        source = kb.create_task(conn, title='source window stopped', assignee='default',
                                session_id=chat, require_scope_authority=True)
        child = kb.create_task(conn, title='dependent ladder', assignee='default',
                               session_id=chat, parents=[source], require_scope_authority=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (source,))
            kb._append_event(conn, source, 'spend_budget_triaged',
                             {'cause': 'scope_stopped', 'reason': 'bounded window ended'})
        dispatch._run_reclaim_phase(conn, dispatch.DispatchResult(), stale_timeout_seconds=0,
                                    failure_limit=2, reconcile_orphans=False, dry_run=False)
        assert [e for e in kb.list_events(conn, source) if e.kind == 'spend_budget_decision_ready']
        assert kb.get_task(conn, source).status == 'triage'
        assert kb.get_task(conn, child).status == 'todo'
        assert conn.execute('SELECT parent_id FROM task_links WHERE child_id=?', (child,)).fetchone()[0] == source
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2
