"""Independent review lifecycle probes for PR 4728."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_auto_replan as rp
from tests.hermes_cli import test_kanban_auto_replan as helpers

kanban_home = helpers.kanban_home

@pytest.mark.parametrize('disposition', ['keep', 'archive', 'delete'])
def test_published_plan_baseline_survives_parent_retirement(kanban_home, disposition):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = rp.hold(conn, tid)
        assert kb.complete_task(conn, parent, result='Concrete new plan: measure then repair.')
        kb.recompute_ready(conn)
        assert rp.hold(conn, tid) is None
        assert rp.state(conn, tid).count == 0
        if disposition == 'archive':
            assert kb.archive_task(conn, parent)
        elif disposition == 'delete':
            assert kb.delete_task(conn, parent)
        current = rp.state(conn, tid)
        assert current.count == 0, current
        assert not current.due, current

@pytest.mark.parametrize('plan', ['', '   ', '\n\t'])
def test_blank_completed_plan_gets_replacement(kanban_home, plan):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = rp.hold(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done', result=? WHERE id=?", (plan, parent))
        kb.recompute_ready(conn)
        replacement = rp.hold(conn, tid)
        assert replacement and replacement != parent
        assert rp.state(conn, tid).count == 8
