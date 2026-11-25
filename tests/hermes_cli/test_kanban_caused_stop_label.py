"""Caused stops are labelled as a worker stopping itself, never as a reached spend budget. [daveey]"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_spend_decision as decision

CAUSES = ['scope_stopped', 'capability_unavailable', 'safety_refusal']


def _label(cause):
    return f'stopped itself: {cause}; awaiting a decision'


@pytest.mark.parametrize('cause', CAUSES)
def test_every_caused_notice_leads_with_its_label_not_a_budget(cause):
    text = decision.notice({'cause': cause, 'task_id': 't_x'})
    assert text.startswith(_label(cause) + '. ')
    assert 'budget reached' not in text.lower() and 'spend budget' not in text.lower()


def test_caused_labels_keep_detailed_safety_and_scope_guidance():
    scope = decision.notice({'cause': 'scope_stopped', 'task_id': 'legacy'})
    assert 'No recorded renewable agent authority' in scope
    assert 'hermes kanban renew-scope' not in scope
    safety = decision.notice({'cause': 'safety_refusal', 'refused_providers': ['a', 'b']})
    assert 'retried unchanged on other eligible providers until every one refused (a, b)' in safety
    assert 'no supported execution release' in safety and 'Do not rerun the refused work.' in safety
    assert 'kanban release' not in safety


def test_capability_stop_keeps_its_own_reason_and_the_decision_guidance():
    text = decision.notice({'cause': 'capability_unavailable', 'reason': 'needs gpu:a100', 'task_id': 't_c'})
    assert text.startswith(_label('capability_unavailable') + '. needs gpu:a100. Held in triage until')
    assert 'Do not requeue unchanged' in text


@pytest.mark.parametrize('payload', [{'reason': 'Spend budget: 15 runs'}, {}, {'cause': 'runs', 'reason': 'cap'},
                                     {'cause': ['scope_stopped']}])
def test_ordinary_notices_are_unchanged(payload):
    text = decision.notice({**payload, 'task_id': 't_o'})
    assert 'stopped itself' not in text
    assert text.startswith(str(payload.get('reason') or 'Spend budget reached') + '. Held in triage until')


def test_creator_notice_keeps_the_stop_reason_after_the_label():
    scope = decision.creator_notice({'cause': 'scope_stopped', 'reason': 'window ended', 'task_id': 't_k'})
    assert scope.startswith(_label('scope_stopped') + '. window ended. No recorded renewable agent authority')
    assert decision.creator_notice({'cause': 'safety_refusal'}) == decision.notice({'cause': 'safety_refusal'})
    capability = {'cause': 'capability_unavailable', 'reason': 'needs gpu:a100'}
    assert decision.creator_notice(capability) == decision.notice(capability)
    assert decision.creator_notice({'reason': 'cap'}) == 'cap. ' + decision.notice({'reason': 'cap'})
    assert decision.creator_notice({}) == 'Hold. ' + decision.notice({})


def test_wake_status_uses_the_newest_notice_cause():
    ordinary = 'reached its spend budget; awaiting a promote or archive decision'
    assert decision.wake_status([{'reason': 'cap'}]) == ordinary
    assert decision.wake_status([]) == ordinary
    assert decision.wake_status([None]) == ordinary
    assert decision.wake_status([{}, {'cause': 'scope_stopped'}]) == _label('scope_stopped')
    assert decision.wake_status([{'cause': 'scope_stopped'}, {'reason': 'cap'}]) == ordinary
    assert decision.wake_status([{'cause': 'safety_refusal'}, None]) == _label('safety_refusal')


@pytest.mark.parametrize('cause', CAUSES)
def test_gateway_ping_and_session_notice_carry_the_label(cause):
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS
    from tui_gateway.session_notifications import _kb_spend_budget
    payload = {'cause': cause, 'reason': 'Spend budget reached'}
    text, body, _ = _EVENT_FORMATTERS[decision.NOTICE](SimpleNamespace(payload=payload), SimpleNamespace(head='card'))
    assert text == f'⏸ card {body}' and body.startswith(_label(cause) + '. ')
    assert _kb_spend_budget(SimpleNamespace(id='t_s'), payload, 'title').startswith(': ' + _label(cause) + '. ')


@pytest.mark.parametrize('cause, expected', [('scope_stopped', _label('scope_stopped')),
                                             (None, 'reached its spend budget; awaiting a promote or archive decision')])
def test_wake_turn_status_names_the_cause(cause, expected):
    from gateway.kanban_watchers_notifier import _KanbanNotification
    payload = {'cause': cause, 'reason': 'Spend budget reached'} if cause else {'reason': 'Spend budget reached'}
    ev = SimpleNamespace(id=1, kind=decision.NOTICE, payload=payload)
    n = SimpleNamespace(task=SimpleNamespace(assignee='w', session_id=None, id='t_w'), sub={'task_id': 't_w', 'chat_id': 'c'},
                        d={'events': [ev]}, wake_agent=True, is_push_adapter=False, title='T', board_slug='b',
                        wake_handoff=None, wake_review_detail=None)
    _KanbanNotification.build_wake_text(n)
    assert n.synth.startswith(f'[kanban] Task t_w {expected}.\n')


def test_label_is_localized(monkeypatch):
    monkeypatch.setenv('HERMES_LANGUAGE', 'de')
    assert decision.caused_label('scope_stopped') == 'hat sich selbst gestoppt: scope_stopped; wartet auf eine Entscheidung'
    assert decision.notice({'cause': 'scope_stopped'}).startswith('hat sich selbst gestoppt: scope_stopped;')
