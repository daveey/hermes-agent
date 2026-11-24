"""Advisory folding must not consume the full completion/review handoff."""
from types import SimpleNamespace
import json

import pytest

from gateway import kanban_wake_fold as fold

@pytest.mark.parametrize('advisory', ['parent_held', 'wait_cycle_decision'])
@pytest.mark.parametrize('handoff', ['completed', 'review_requested'])
@pytest.mark.parametrize('advisory_first', [True, False])
@pytest.mark.parametrize('unreadable', [True, False])
def test_advisory_and_full_handoff_survive_fold_and_claim_refresh(
        monkeypatch, advisory, handoff, advisory_first, unreadable):
    def live(card):
        if unreadable:
            raise OSError('board unavailable')
        return ('done' if handoff == 'completed' else 'review'), ''

    monkeypatch.setattr(fold, '_live_task', live)
    note = 'Held parent remains stopped; inspect its decision owner.'
    body = ('Summary: first line\nsecond line of the handoff\n'
            'Changed files: gateway/example.py\nTests: regression passed')
    advisory_card = dict(task_id='t_fixture', board='', kinds=[advisory], notices=[note],
                         text=f'[kanban] Task t_fixture {advisory}.\nTitle: fixture\n{note}')
    handoff_card = dict(task_id='t_fixture', board='', kinds=[handoff],
                        text=f'[kanban] Task t_fixture {handoff}.\nTitle: fixture\n{body}')
    first, second = ((advisory_card, handoff_card) if advisory_first
                     else (handoff_card, advisory_card))
    waiting = dict(request_id=json.dumps(['kanban-wake', 'a']), payload=dict(
        text=first['text'], local_automation_v1=dict(kanban_cards=[first])))
    event = SimpleNamespace(text=second['text'], metadata=dict(kanban_cards=[second]))
    fold.fold_into(event, waiting, json.dumps(['kanban-wake', 'b']))
    assert body in event.text
    assert event.text.count(note) == 1
    [card] = fold.card_metadata(event.metadata)['kanban_cards']
    payload = dict(text=event.text, local_automation_v1=dict(
        identity=json.dumps(['kanban-wake', 'b']), kanban_cards=[card]))
    fold.refresh_claimed(payload)
    assert body in payload['text']
    assert payload['text'].count(note) == 1
    assert handoff in card['kinds']
    # Re-folding the advisory cannot consume the body or duplicate its notice.
    [again] = fold._merge([card], [advisory_card])
    assert body in again['text']
    assert again['text'].count(note) == 1


def test_ordinary_later_block_still_supersedes_completion():
    done = dict(task_id='t_fixture', board='', kinds=['completed'], text='old completion')
    blocked = dict(task_id='t_fixture', board='', kinds=['blocked'], text='new user decision')
    assert fold._merge([done], [blocked]) == [blocked]
