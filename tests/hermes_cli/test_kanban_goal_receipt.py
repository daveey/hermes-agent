"""Only a successful judge of the current handoff releases goal work."""
import argparse
import json

import pytest

from hermes_cli import kanban as cli

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tools import kanban_tools


def test_successful_goal_gate_releases_completed_work(board, monkeypatch):
    monkeypatch.setattr(kanban_tools, '_goal_judge_available', lambda: True)
    monkeypatch.setattr(kanban_tools, 'judge_goal', lambda **kw: ('done', 'verified', None, None, False))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default', goal_mode=True,
                             completion_contract='local-only')
        proof = kanban_tools._goal_gate('kanban_complete', kb.get_task(conn, tid), tid, 'Tested')
        assert kb.complete_task(conn, tid, summary='Tested', goal_verification=proof)
        conn.execute('BEGIN')
        observed = ledger.observe_owner_task(conn, {'id': tid, 'status': 'done'}, now=1000)
        assert observed['origin_observation']['verified'] is True


@pytest.mark.parametrize('entry', ['tool', 'cli'])
@pytest.mark.parametrize('failure', ['parse', 'transport', 'exception', 'unavailable', 'review'])
def test_no_verification_from_failed_judge_or_review(board, monkeypatch, entry, failure):
    monkeypatch.setattr(kanban_tools, '_goal_judge_available', lambda: failure != 'unavailable')
    monkeypatch.setattr('agent.auxiliary_client.get_text_auxiliary_client',
                        lambda *a: (None, None) if failure == 'unavailable' else (object(), 'judge'))
    def judge(**kw):
        if failure == 'exception':
            raise RuntimeError('offline')
        return ('done', 'result', failure == 'parse', None, failure == 'transport')
    monkeypatch.setattr(kanban_tools, 'judge_goal', judge)
    monkeypatch.setattr('hermes_cli.goals.judge_goal', judge)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default', goal_mode=True, completion_contract='local-only')
        task = kb.get_task(conn, tid)
        handoff = 'kanban_request_review' if failure == 'review' else 'kanban_complete'
        if entry == 'tool':
            proof = kanban_tools._goal_gate(handoff, task, tid, 'Tested')
        else:
            verified = []
            assert cli._goal_mode_handoff_rejection(task, 'Tested', handoff, verified=verified) == ('done', None)
            proof = verified[0] if verified else None
        assert kb.complete_task(conn, tid, summary='Tested', goal_verification=proof)
        conn.execute('BEGIN')
        assert not ledger.observe_owner_task(conn, {'id': tid, 'status': 'done'}, now=1000)['origin_observation']['verified']


@pytest.mark.parametrize('change', ['title', 'body', 'evidence', 'status', 'metadata'])
def test_changed_subject_or_worker_metadata_cannot_release(board, monkeypatch, change):
    monkeypatch.setattr(kanban_tools, '_goal_judge_available', lambda: True)
    monkeypatch.setattr(kanban_tools, 'judge_goal', lambda **kw: ('done', 'verified', False, None, False))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default', goal_mode=True, completion_contract='local-only')
        proof = kanban_tools._goal_gate('kanban_complete', kb.get_task(conn, tid), tid, 'Tested')
        if change in ('title', 'body'):
            conn.execute(f'UPDATE tasks SET {change}=? WHERE id=?', ('Changed', tid))
        elif change == 'status':
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        evidence = 'Different' if change == 'evidence' else 'Tested'
        assert kb.complete_task(conn, tid, summary=evidence,
                                goal_verification=None if change == 'metadata' else proof,
                                metadata={'goal_verified_event_id': 1, 'goal_verified': True})
        conn.execute('BEGIN')
        assert not ledger.observe_owner_task(conn, {'id': tid, 'status': 'done'}, now=1000)['origin_observation']['verified']


@pytest.mark.parametrize('entry', ['tool', 'cli'])
def test_completion_entry_records_real_goal_success(board, monkeypatch, entry):
    monkeypatch.setattr(kanban_tools, '_goal_judge_available', lambda: True)
    monkeypatch.setattr(kanban_tools, 'judge_goal', lambda **kw: ('done', 'verified', False, None, False))
    monkeypatch.setattr('agent.auxiliary_client.get_text_auxiliary_client', lambda *a: (object(), 'judge'))
    monkeypatch.setattr('hermes_cli.goals.judge_goal', lambda **kw: ('done', 'verified', False, None, False))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Build', assignee='default', goal_mode=True,
                             completion_contract='local-only')
    if entry == 'cli':
        assert cli._cmd_complete(argparse.Namespace(task_ids=[tid], summary='Tested', result=None, metadata=None)) == 0
    else:
        assert json.loads(kanban_tools._handle_complete({'task_id': tid, 'summary': 'Tested'}))['ok']
    with kbc.connect_closing() as conn:
        conn.execute('BEGIN')
        assert ledger.observe_owner_task(conn, {'id': tid, 'status': 'done'}, now=1000)['origin_observation']['verified']
