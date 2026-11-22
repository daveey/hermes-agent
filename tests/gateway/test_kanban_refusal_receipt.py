"""The real worker-result writer must preserve typed safety evidence, never prose."""
from __future__ import annotations

import json
import os

import pytest

from agent.turn_failure_copy import content_policy_blocked_result
from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_PROFILE', 'default')
    for name in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_TASK',
                 'HERMES_KANBAN_RUN_ID', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(name, raising=False)
    db = kb.kanban_db_path()
    kb.init_db()
    with connect_closing() as conn:
        tid = kb.create_task(conn, title='isolated refusal receipt', assignee='default')
        assert kb.claim_task(conn, tid, claimer='host:fixture')
        task = kb.get_task(conn, tid)
        assert task is not None
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'worker_bound',
                             {'pid': os.getpid(), 'claim_lock': task.claim_lock},
                             run_id=task.current_run_id)
    return {'db': str(db), 'task_id': tid, 'run_id': task.current_run_id,
            'claim_lock': task.claim_lock}


def _result(worker, failure):
    session_kanban._record_worker_result(worker, 1, 'worker failed', failure)
    with connect_closing() as conn:
        rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result'",
                            (worker['task_id'],)).fetchall()
        assert len(rows) == 1
        return json.loads(rows[0]['payload'])


def test_typed_refusal_survives_the_actual_worker_writer(worker):
    failure = content_policy_blocked_result(
        [], 1, final_response='display only', error_detail='private provider prose',
        refusal_category='cyber_safety', ran_actions=True)
    result = _result(worker, failure)
    assert result['failed'] is True
    assert result['failure_reason'] == 'content_policy_blocked'
    assert result['failure_retryable'] is False
    assert result['refusal_category'] == 'cyber_safety'
    assert result['refused_turn_ran_actions'] is True
    assert not {'messages', 'error', 'refused_turn_notice', 'final_response'} & result.keys()
    assert 'private provider prose' not in json.dumps(result)


@pytest.mark.parametrize('failure', [
    {'failure_reason': 'content_policy_blocked', 'failed': False},
    {'failure_reason': 'content_policy_blocked', 'failed': 1},
    {'failed': True, 'failure_reason': 'rate_limit', 'failure_resets_at': 1999999999},
    {'failed': True, 'error': 'content_policy_blocked: quoted text'},
    RuntimeError('content_policy_blocked'),
])
def test_prose_and_unproven_failures_do_not_mint_refusal(worker, failure):
    result = _result(worker, failure)
    assert result.get('failure_reason') != 'content_policy_blocked'
    assert 'refusal_category' not in result


def test_invalid_category_is_not_copied(worker):
    failure = content_policy_blocked_result(
        [], 1, final_response='', error_detail='', refusal_category='<private provider text>')
    result = _result(worker, failure)
    assert result['failure_reason'] == 'content_policy_blocked'
    assert 'refusal_category' not in result
