"""Scope decisions cannot multiply their own administrative recovery tree."""
import json
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_containment_actor as support

board = support.board


def test_admin_exhaustion_routes_to_existing_root_not_another_card(board, monkeypatch):
    root, _, _ = support.bind(board, monkeypatch)
    admin = kb.create_task(board, title='Administrative scope decision', assignee='default', created_by='fixture')
    kb._append_event(board, admin, 'administrative_decision', {'role': 'administrative_decision', 'root_task': root, 'root_episode': 'root-stop'})
    kb._append_event(board, admin, decision.EVENT, {'reason': 'Decision budget exhausted'})
    count = board.execute('SELECT count(*) FROM tasks').fetchone()[0]
    decision.ensure_producer(board, admin)
    decision.ensure_producer(board, admin)
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == count
    notices = board.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?', (admin, decision.NOTICE)).fetchall()
    assert len(notices) == 1
    assert json.loads(notices[0][0])['producer'] == {'kind': 'unavailable', 'root_task': root, 'root_episode': 'root-stop', 'error': 'administrative_owner_exhausted'}
