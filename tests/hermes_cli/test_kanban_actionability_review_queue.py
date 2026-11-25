"""A card queued for its reviewer is runnable, not an administrative wait; holds remain visible."""
from __future__ import annotations

import pytest

from hermes_cli.kanban_actionability import project

NOT_REQUIRED = {'at': None, 'route': 'existing_decision_requested', 'state': 'not_required', 'receipt_uid': None}


def _review(**extra):
    return project({'task': {'id': 'handoff', 'status': 'review', 'assignee': 'default'},
                    'review_dispatch': True, **extra}, now=1000)


@pytest.mark.parametrize('fact', [{}, {'review_dispatch': False}, {'review_dispatch': None}, {'review_dispatch': 1},
                                  {'review_dispatch': 'true'}])
def test_review_row_without_established_dispatch_keeps_its_missing_deadline(fact):
    """Pure project default: no reviewer admission is assumed unless the caller supplies exactly True."""
    row = project({'task': {'id': 'handoff', 'status': 'review', 'assignee': 'default'}, **fact}, now=1000)
    assert row['action'] == 'review_current_scope' and row['blocker'] is None
    assert row['escalation']['state'] == 'unavailable'
    assert row['violations'] == ['escalation_deadline_missing']


def test_review_queue_needs_no_administrative_deadline():
    row = _review()
    assert row['action'] == 'review_current_scope'
    assert row['blocker'] is None
    assert row['evidence_state'] == 'unknown'
    assert row['escalation'] == NOT_REQUIRED
    assert row['violations'] == []


@pytest.mark.parametrize('restriction', [
    {'stop': {'cause': 'scope_stopped'}},
    {'containment': 'episode'},
    {'human_step': 'Owner must sign in'},
    {'dependencies': [{'task_id': 'parent', 'status': 'todo', 'trigger': 'Parent completes'}]},
    {'lane_hold': True},
])
def test_review_status_cannot_hide_an_administrative_wait(restriction):
    row = _review(**restriction)
    assert row['blocker'] is not None
    assert 'escalation_deadline_missing' in row['violations']
    assert row['escalation']['state'] == 'unavailable'


@pytest.mark.parametrize('extra,violation', [
    ({'private_hold': 'scope_stop_not_renewed'}, 'private_hold'),
    ({'invalid_creation_intent': True}, 'creation_intent_invalid'),
    ({'administrative_owner': {'kind': 'unavailable', 'refs': []}}, 'owner_missing'),
    ({'pending_command': {'request_uid': 'unsettled'}}, 'escalation_deadline_missing'),
])
def test_review_exemption_preserves_unresolved_custody(extra, violation):
    row = _review(**extra)
    assert violation in row['violations']
    assert row['escalation']['state'] == 'unavailable'


def test_review_card_without_an_assignee_is_still_flagged():
    row = project({'task': {'id': 'handoff', 'status': 'review', 'assignee': None}}, now=1000)
    assert row['escalation']['state'] == 'unavailable'
    assert 'escalation_deadline_missing' in row['violations']


def test_existing_review_deadline_is_not_erased():
    row = _review(deadline_at=2000)
    assert row['escalation']['at'] == 2000
    assert row['escalation']['state'] == 'pending'


def _config(monkeypatch, value):
    from hermes_cli import config

    def load():
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(config, 'load_config', load)


@pytest.mark.parametrize('config', [
    {'kanban': {'review_dispatch': False}},
    {'kanban': {'review_dispatch': 'yes'}},
    {'kanban': {}},
    {'kanban': None},
    {},
    None,
    RuntimeError('config unreadable'),
])
def test_review_handoff_on_a_board_without_review_dispatch_stays_flagged(tmp_path, monkeypatch, config):
    """Human-only review, or a setting that cannot be read: no dispatcher admits a reviewer, so the wait has no clock."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_actionability_cli import report
    from hermes_cli.kanban_actionability_sources import describe
    from hermes_cli.kanban_db_connect import connect
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='implemented work', assignee='default')
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.request_review(conn, tid, summary='implemented; please review',
                                 expected_run_id=claimed.current_run_id) is True
        _config(monkeypatch, config)
        listed = {r['task_id']: r for r in report(conn)['rows']}
        for row in (describe(conn, tid), listed[tid]):
            assert row['status'] == 'review' and row['action'] == 'review_current_scope'
            assert row['escalation']['state'] == 'unavailable'
            assert row['violations'] == ['escalation_deadline_missing']


def test_real_review_handoff_reaches_describe_and_report(tmp_path, monkeypatch):
    """The census path: review_requested from a live run, read before any reviewer claims it."""
    from hermes_cli import kanban_db as kb
    _config(monkeypatch, {'kanban': {'review_dispatch': True}})
    from hermes_cli.kanban_actionability_cli import report
    from hermes_cli.kanban_actionability_sources import describe
    from hermes_cli.kanban_db_connect import connect
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='implemented work', assignee='default')
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.request_review(conn, tid, summary='implemented; please review',
                                 expected_run_id=claimed.current_run_id) is True
        assert kb.get_task(conn, tid).status == 'review'
        listed = {r['task_id']: r for r in report(conn)['rows']}
        for row in (describe(conn, tid), listed[tid]):
            assert row['status'] == 'review'
            assert row['action'] == 'review_current_scope' and row['blocker'] is None
            assert row['escalation'] == NOT_REQUIRED
            assert row['violations'] == []
            assert row['evidence_state'] == 'unknown'


def test_review_card_behind_a_late_parent_is_still_flagged(tmp_path):
    """A prerequisite linked onto a card in review is a dependency wait, not a runnable queue."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_actionability_sources import describe
    from hermes_cli.kanban_db_connect import connect
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='implemented work', assignee='default')
        assert kb.request_review(conn, tid, summary='implemented; please review') is True
        parent = kb.create_task(conn, title='late prerequisite', assignee='default')
        kb.link_tasks(conn, parent, tid)
        row = describe(conn, tid)
        assert row['status'] == 'review' and row['action'] == 'satisfy_dependency'
        assert row['blocker'] is not None
        assert 'escalation_deadline_missing' in row['violations']
