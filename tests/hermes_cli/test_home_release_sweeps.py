"""Sibling budget/blocked release paths preserve frozen rows and peer progress."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from tests.hermes_cli import test_home_release_candidates as support

board = support.board


@pytest.mark.parametrize("phase", support.PHASES)
@pytest.mark.parametrize("operation", ("spend", "no_progress", "review_refusal", "spend_producer"))
def test_preclaim_accounting_is_write_free_for_transfers(board, monkeypatch, phase, operation):
    from hermes_cli import kanban_spend_budget as budget, kanban_no_progress as progress
    from hermes_cli import kanban_spend_decision as decision, kanban_review_admission as review
    task = support.card(board, "ready")
    with dbc.write_txn(board):
        kb._append_event(board, task, decision.EVENT, {"reason": "prior budget stop"})
    support.freeze(board, task, phase)
    before = board.total_changes
    if operation == "spend":
        assert budget.hold(board, task) is None
    elif operation == "no_progress":
        monkeypatch.setattr(progress, "decide", lambda *a, **k: ("escalate", 99, 0, None))
        assert progress.hold(board, task) is None
    elif operation == "review_refusal":
        result = SimpleNamespace(auto_blocked=[])
        review.record_refusal(board, task, RuntimeError("unavailable"), result, lane="ready")
        assert result.auto_blocked == []
    else:
        decision.ensure_producer(board, task)
    assert board.total_changes == before
    assert kb.get_task(board, task).status == "ready"


@pytest.mark.parametrize("phase", support.PHASES)
@pytest.mark.parametrize("operation", ("agent_block", "standing_ask"))
def test_blocked_sweeps_skip_transfers_without_starving_peer(board, monkeypatch, phase, operation):
    from hermes_cli import kanban_block_policy as policy
    task, neighbour = support.card(board, "blocked"), support.card(board, "blocked")
    with dbc.write_txn(board):
        for tid in (task, neighbour):
            board.execute("UPDATE tasks SET block_kind='transient' WHERE id=?", (tid,))
            if operation == "standing_ask":
                board.execute("UPDATE tasks SET human_request='{}' WHERE id=?", (tid,))
                board.execute("UPDATE tasks SET human_request=? WHERE id=?", ('{"action":"old ask"}', tid))
    support.freeze(board, task, phase)
    row = tuple(board.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
    if operation == "agent_block":
        moved = policy.sweep_agent_owned_blocks(board, grace=0)
    else:
        monkeypatch.setattr(policy, "standing_ask_error", lambda *a: ("test", "premise gone", {}))
        moved = policy.sweep_standing_asks(board)
    assert moved == [neighbour]
    assert tuple(board.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()) == row
    assert kb.get_task(board, neighbour).status == "triage"


@pytest.mark.parametrize("phase", support.PHASES)
def test_spend_reviewer_due_and_grant_exclude_transfer(board, monkeypatch, phase):
    from hermes_cli import kanban_spend_reviewer as review, kanban_spend_decision as decision
    task, neighbour = support.card(board), support.card(board)
    episodes = {}
    with dbc.write_txn(board):
        for tid in (task, neighbour):
            kb._append_event(board, tid, decision.EVENT, {"reason": "ordinary budget stop"})
            episodes[tid] = review.episode_of(decision.pending_episode(board, tid))
            kb._append_event(board, tid, review.QUEUED, {"episode": episodes[tid]})
    support.freeze(board, task, phase)
    monkeypatch.setattr(review, "settings", lambda: (True, 5))
    before = board.total_changes
    assert review._due(board, 9999999999, set()) == [(neighbour, episodes[neighbour], 0)]
    assert review.grant(board, task, episodes[task], 1, "progress", "evidence") is not None
    monkeypatch.setattr(review, "judge", lambda *a: pytest.fail("frozen target reached judge"))
    assert review.review(board, task, episodes[task]) == "moot"
    assert review.escalate(board, task, episodes[task], "unavailable") == "moot"
    assert board.total_changes == before
    assert kb.get_task(board, task).status == "triage"
