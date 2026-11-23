"""One attention contract for lane reasons and card presentation."""
import json

import pytest

from hermes_cli.kanban_lanes_flow import classify_reason, classify_step


HR = {'action': 'Choose a route', 'why_human': 'Only you know', 'interaction': 'reply',
      'scope': 'This card', 'evidence': 'A choice is needed'}


@pytest.mark.parametrize(('card', 'expected'), [
    ({'status': 'blocked', 'block_kind': 'needs_input', 'human_request': HR}, True),
    ({'status': 'triage', 'block_kind': 'capability', 'human_request': json.dumps(HR)}, True),
    ({'status': 'blocked', 'block_kind': 'needs_input'}, False),
    ({'status': 'scheduled', 'block_kind': 'capability'}, False),
    ({'status': 'running', 'human_request': HR}, False),
    ({'status': 'done', 'wait': {'wait_state': 'human'}}, False),
    ({'status': 'blocked', 'human_request': {'action': 'incomplete'}}, False),
    ({'status': 'blocked', 'stale': True, 'human_request': HR}, False),
    ({'status': 'scheduled', 'wait': {'wait_state': 'human', 'owner_step': 'Connect device'}}, True),
    ({'status': 'scheduled', 'events': [{'kind': 'scheduled', 'payload': {
        'wake_check': 'python3 /example/probe_phone_available.py'}}]}, True),
    ({'status': 'scheduled', 'events': [{'kind': 'scheduled', 'payload': {
        'wake_check': 'python3 /example/passive_turn_capture.py'}}]}, False),
])
def test_needs_you_contract(card, expected):
    from hermes_cli.kanban_open_asks import needs_you

    assert needs_you(card) is expected
    assert (classify_reason(card, classify_step(card)) == 'you') is expected


def test_blocked_to_scheduled_drops_stale_kind():
    from hermes_cli.kanban_open_asks import needs_you

    card = {'status': 'blocked', 'block_kind': 'capability', 'human_request': HR}
    assert needs_you(card)
    card.update(status='scheduled', human_request=None)
    assert not needs_you(card)
    assert classify_reason(card, 'waiting') == 'time'
