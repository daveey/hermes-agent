"""Historical malformed events must not abort unrelated decision delivery."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home




@pytest.mark.parametrize('raw', ['{broken', 'null', '[]', '"text"'])
def test_malformed_stop_still_has_conservative_notice(kanban_home, raw):
    from hermes_cli import kanban_hold_notices as notices
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        tid = kb.create_task(conn, title='legacy hold', assignee='default', session_id=chat,
                             require_scope_authority=True)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'spend_budget_triaged', {})
            conn.execute('UPDATE task_events SET payload=? WHERE id=(SELECT MAX(id) FROM task_events)', (raw,))
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        notices.sweep(conn)
        decision.ensure_producer(conn, tid)
        events = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?', (tid, decision.NOTICE)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])['episode']
        assert kb.get_task(conn, tid).status == 'triage'


def test_old_and_tick_producers_coexist_without_duplicate_episode(kanban_home):
    from hermes_cli import kanban_hold_notices as notices
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        tid = kb.create_task(conn, title='bounded', assignee='default', session_id=chat,
                             require_scope_authority=True)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, decision.EVENT, {'reason': 'run limit'})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        notices.sweep(conn)
        decision.ensure_producer(conn, tid)
        notices.sweep(conn)
        decision.ensure_producer(conn, tid)
        assert len(conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?', (tid, decision.NOTICE)).fetchall()) == 1
        assert len(kb.list_tasks(conn)) == 1  # no supervisor/guard fallback




@pytest.mark.parametrize('providers', [1, True, {'wrong': 1}])
def test_safety_notice_does_not_iterate_malformed_provider_collection(providers):
    from hermes_cli.kanban_spend_decision import notice
    result = notice({'cause': 'safety_refusal', 'refused_providers': providers})
    assert 'no supported execution release' in result


