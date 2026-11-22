"""Administrative comments and review metadata cannot renew restricted-scope spend."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_retain_local as support
from gateway.session_context import scoped_current_session_id

board = support.board


@pytest.mark.parametrize('cause', ['scope_stopped', 'safety_refusal'])
@pytest.mark.parametrize('after', [200, 300])
def test_actual_human_comment_preserves_window_and_post_stop_usage(board, monkeypatch, cause, after):
    from hermes_cli import kanban_retain as retain
    tid, rid, session = support.support.bind(board, monkeypatch)
    # An earlier genuine human authorization is preserved.
    kb.add_comment(board, tid, 'fixture-human', 'Prior authorized decision', writer_kind='human')
    board.execute("UPDATE task_events SET created_at=100 WHERE task_id=? AND kind='commented'", (tid,))
    with scoped_current_session_id(session):
        retain.retain(board, board='default', **support.ARGS)
    board.execute("UPDATE task_events SET created_at=200 WHERE task_id=? AND kind IN ('spend_budget_triaged','decision_retained')", (tid,))
    if cause == 'safety_refusal':
        kb._append_event(board, tid, 'decision_hold_restricted', {'cause':cause,'accounting_boundary':False}, run_id=rid)
        board.execute("UPDATE task_events SET created_at=200 WHERE task_id=? AND kind='decision_hold_restricted'", (tid,))
    board.execute('UPDATE task_runs SET started_at=150,ended_at=250 WHERE id=?', (rid,))
    kb._append_event(board, tid, 'worker_result', {'usage':{'cache_read_tokens':1234}}, run_id=rid)
    before = budget.spend(board, tid, as_of=400)
    cid = kb.add_comment(board, tid, 'fixture-human', 'Administrative note only', writer_kind='human')
    board.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='commented' AND json_extract(payload,'$.comment_id')=?", (after,tid,cid))
    kb._append_event(board, tid, 'promoted_manual', {'spend_budget':{'runs':3}})
    board.execute("UPDATE task_events SET created_at=300 WHERE task_id=? AND kind='promoted_manual'", (tid,))
    after_spend = budget.spend(board, tid, as_of=400)
    assert before.since == after_spend.since == 100
    assert after_spend.runs == before.runs == 1
    assert after_spend.tokens['cache_read_tokens'] == before.tokens['cache_read_tokens'] == 1234
