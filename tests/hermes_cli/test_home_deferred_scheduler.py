"""Actual due/start/thread recovery of a never-submitted wait decision. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_spend_review_request as journal
from tests.hermes_cli import test_home_review_composed_delivery as cases

board = cases.board
hub = cases.hub


@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_native_due_thread_retries_same_decision_after_release(board, spend_reviewer, hub, phase):
    target = cases.support.card(board, 'ready')
    source = kb.create_task(board, title='caused stop with prerequisite', assignee='default')
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (source,))
        kb._append_event(board, source, 'spend_budget_triaged',
                         {'cause': 'scope_stopped', 'reason': f'Stopped until {target} lands.'})
    episode = reviewer._current_episode(board, source)
    assert episode is not None
    with kb.write_txn(board):
        kb._append_event(board, source, decision.NOTICE, {'episode': episode})
        kb._append_event(board, source, reviewer.STARTED, {'episode': episode})
    cases.support.freeze(board, target, phase)
    spend_reviewer.append({'decision': 'wait_on', 'reason': f'Blocked on {target}.', 'target': target})
    assert reviewer.review(board, source, episode) == 'pending'
    semantic = journal.get(board, source, episode)['wire']
    assert reviewer.start_due(board, now=10**10) == []
    cases.unfreeze(board, target, phase)
    assert reviewer.start_due(board, now=10**10) == [source]
    reviewer.wait_for_reviews(timeout=30)
    assert reviewer.state(board, source, episode) == reviewer.SETTLED
    assert journal.get(board, source, episode)['wire'] == semantic
    assert len(hub) == 1 and not spend_reviewer
    assert {k: v for k, v in hub[0].items() if k != 'home_generations'} == semantic
    assert reviewer.start_due(board, now=10**10 + 1000) == []
