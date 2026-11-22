"""Real admitted local lifecycle: retain stops a run, not its acceptance or spend."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_containment_actor as support
from gateway.session_context import scoped_current_session_id

board = support.board
ARGS = dict(summary='Incomplete; no execution authority', reason='scope_stopped',
            next_step='Owning supervisor records scope decision', release_condition='Supported explicit authority',
            evidence=['synthetic stopped-scope fixture'])


def test_registered_retain_ends_exact_run_without_dependency_or_window_reset(board, monkeypatch):
    from tools import kanban_tools as tools
    from tools.registry import registry
    from agent.kanban_stop import session_called_kanban_terminal
    tid, rid, session = support.bind(board, monkeypatch)
    monkeypatch.setattr(tools, '_board', lambda *args: scope(board))
    before = budget._window_start(board, tid, 2999999999)
    handler = registry.get_entry('kanban_retain').handler
    with scoped_current_session_id(session):
        result = json.loads(handler(ARGS))
    assert result['terminal_for_run'] is True and result['dependency_satisfied'] is False
    assert kb.get_task(board, tid).status == 'triage'
    assert board.execute('SELECT outcome FROM task_runs WHERE id=?', (rid,)).fetchone()[0] == 'retained'
    assert decision.pending(board, tid)
    assert budget._window_start(board, tid, 2999999999) == before
    assert session_called_kanban_terminal([{'role': 'tool', 'name': 'kanban_retain', 'content': json.dumps(result)}])
    assert not session_called_kanban_terminal([{'role': 'tool', 'name': 'kanban_retain', 'content': '{"ok":false}'}])
    with scoped_current_session_id(session):
        replay = json.loads(handler(ARGS))
    assert replay['event_uid'] == result['event_uid']
    assert board.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='decision_retained'", (tid,)).fetchone()[0] == 1


def scope(board):
    from contextlib import nullcontext
    return nullcontext(('default', board))


@pytest.mark.parametrize('reason', ['decision_pending', 'unsupported'])
def test_invalid_or_absent_decision_never_ends_run(board, monkeypatch, reason):
    from hermes_cli import kanban_retain as retain
    tid, rid, session = support.bind(board, monkeypatch)
    with scoped_current_session_id(session), pytest.raises(ValueError):
        retain.retain(board, board='default', **{**ARGS, 'reason': reason})
    assert kb.get_task(board, tid).status == 'running'
    assert board.execute('SELECT ended_at FROM task_runs WHERE id=?', (rid,)).fetchone()[0] is None


def test_scope_restriction_survives_later_ordinary_promotion(board, monkeypatch):
    from hermes_cli import kanban_retain as retain
    tid, _, session = support.bind(board, monkeypatch)
    with scoped_current_session_id(session):
        retain.retain(board, board='default', **ARGS)
    kb._append_event(board, tid, 'promoted_manual', {})
    assert decision.pending(board, tid)
    assert 'scope' in decision.unblock_refusal(board, tid).lower()
    before = budget._window_start(board, tid, 2999999999)
    kb.add_comment(board, tid, 'operator-fixture', 'Administrative note, not release', writer_kind='human')
    assert kb.get_task(board, tid).status == 'triage'
    promoted, refusal = kb.promote_task(board, tid, actor='fixture', reason='generic promotion')
    assert promoted is False
    assert 'scope stopped' in refusal.lower()
    assert 'provider' not in refusal.lower()
    from hermes_cli.kanban_spend_promote import scope_refusal
    refusal = scope_refusal(board, tid, 'decision-fixture', 'episode-fixture', 1)
    assert 'scope stopped' in refusal.lower()
    assert 'provider' not in refusal.lower()
    assert not kb.unblock_task(board, tid)
    assert budget._window_start(board, tid, 2999999999) == before
