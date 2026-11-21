"""Catalog routing sentinels must not turn board references into model pins."""
import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as chelpers

board = chelpers.board


@pytest.mark.parametrize('text', [
    'Supervise each: read the card ... - default/t_ffffffffffffffff is only context here. Complete this card with what you changed.',
    'Use default board for this card.',
    'Use auto placement and preserve independent review.',
    'The default profile is required for the fixture.',
    'default/t_ffffffffffffffff is only context here.',
])
def test_routing_sentinel_context_creates_unchanged(board, text):
    tid = kb.create_task(board, title='Supervision context', body=text)
    task = kb.get_task(board, tid)
    assert task is not None
    assert task.body == text
    assert task.model_override is None and task.provider_override is None


@pytest.mark.parametrize('directive', [
    'Use claude-opus-5-5.',
    'Use gpt-6-luna.',
    'require_model: true',
])
def test_sentinel_context_does_not_hide_active_mandate(board, directive):
    text = 'default/t_ffffffffffffffff is only context here.\n' + directive
    before = board.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError, match='refused line:'):
        kb.create_task(board, title='Supervision context', body=text)
    assert board.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
