"""Runnable ownership is not an administrative wait; holds remain visible."""
from __future__ import annotations

import pytest

from hermes_cli.kanban_actionability import project


@pytest.mark.parametrize('status', ['ready', 'running'])
def test_unblocked_executor_does_not_require_administrative_deadline(status):
    facts = {'task': {'id': 'executor', 'status': status, 'assignee': 'default'},
             'run': {'status': 'running', 'ended_at': None}, 'run_evidence_known': True}
    row = project(facts, now=1000)
    assert row['action'] == 'execute_current_scope'
    assert row['blocker'] is None
    assert row['evidence_state'] == 'unknown'
    assert row['escalation']['at'] is None
    assert row['escalation']['state'] == 'not_required'
    assert 'escalation_deadline_missing' not in row['violations']


@pytest.mark.parametrize('status', ['ready', 'running'])
@pytest.mark.parametrize('restriction', [
    {'stop': {'cause': 'scope_stopped'}},
    {'containment': 'episode'},
    {'human_step': 'Owner must sign in'},
    {'dependencies': [{'task_id': 'parent', 'status': 'todo', 'trigger': 'Parent completes'}]},
    {'lane_hold': True},
])
def test_executor_status_cannot_hide_an_administrative_wait(status, restriction):
    facts = {'task': {'id': 'executor', 'status': status, 'assignee': 'default'},
             'run': {'status': 'running', 'ended_at': None}, 'run_evidence_known': True,
             **restriction}
    row = project(facts, now=1000)
    assert row['blocker'] is not None
    assert 'escalation_deadline_missing' in row['violations']


@pytest.mark.parametrize('extra,violation', [
    ({'private_hold': 'scope_stop_not_renewed'}, 'private_hold'),
    ({'invalid_creation_intent': True}, 'creation_intent_invalid'),
    ({'administrative_owner': {'kind': 'unavailable', 'refs': []}}, 'owner_missing'),
    ({'pending_command': {'request_uid': 'unsettled'}}, 'escalation_deadline_missing'),
])
def test_executor_exemption_preserves_unresolved_custody(extra, violation):
    facts = {'task': {'id': 'executor', 'status': 'ready', 'assignee': 'default'}, **extra}
    row = project(facts, now=1000)
    assert violation in row['violations']
    assert row['escalation']['state'] == 'unavailable'


@pytest.mark.parametrize('status', ['todo', 'review', 'blocked', 'scheduled', 'triage'])
def test_other_nonterminal_states_still_require_deadlines(status):
    row = project({'task': {'id': 'waiting', 'status': status, 'assignee': 'default'}}, now=1000)
    assert 'escalation_deadline_missing' in row['violations']


def test_existing_deadline_is_not_erased():
    row = project({'task': {'id': 'executor', 'status': 'ready', 'assignee': 'default'},
                   'deadline_at': 2000}, now=1000)
    assert row['escalation']['at'] == 2000
    assert row['escalation']['state'] == 'pending'


def test_missing_run_is_not_exempted():
    row = project({'task': {'id': 'lost', 'status': 'running', 'assignee': 'default'},
                   'run_evidence_known': True}, now=1000)
    assert 'running_without_current_run' in row['violations']
    assert 'escalation_deadline_missing' in row['violations']


def test_real_ready_and_claimed_sources_reach_describe_and_report(tmp_path):
    """The census path: an ordinary created/claimed source is runnable, not stranded."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_actionability_cli import report
    from hermes_cli.kanban_actionability_sources import describe
    from hermes_cli.kanban_db_connect import connect
    with connect(tmp_path / 'kanban.db') as conn:
        ready = kb.create_task(conn, title='queued work', assignee='default')
        running = kb.create_task(conn, title='claimed work', assignee='default')
        assert kb.claim_task(conn, running) is not None
        listed = {r['task_id']: r for r in report(conn)['rows']}
        for tid, status in ((ready, 'ready'), (running, 'running')):
            for row in (describe(conn, tid), listed[tid]):
                assert row['status'] == status
                assert row['action'] == 'execute_current_scope' and row['blocker'] is None
                assert row['escalation'] == {'at': None, 'route': 'existing_decision_requested',
                                             'state': 'not_required', 'receipt_uid': None}
                assert row['violations'] == []
                assert row['evidence_state'] == 'unknown'


def test_claimed_card_that_loses_its_run_is_still_flagged(tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_actionability_sources import describe
    from hermes_cli.kanban_db_connect import connect
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='lost executor', assignee='default')
        assert kb.claim_task(conn, tid) is not None
        conn.execute("UPDATE task_runs SET status='crashed', ended_at=1 WHERE task_id=?", (tid,))
        row = describe(conn, tid)
        assert row['blocker'] is not None
        assert 'escalation_deadline_missing' in row['violations']
