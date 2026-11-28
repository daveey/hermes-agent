"""Real isolated Board composition for an agent renewal racing spend review."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_spend_allowance_board as board

composed = board.composed


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_agent_budget_after_review_claims_without_reset(composed, lane):
    home, pool, task = composed
    home.round()
    home.enter()
    before = home.hub.reviewer_allowance(board.BOARD, task)["allowance"]
    seen = home.conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (task,)).fetchone()[0]
    # This is the write made by a release whose earlier preflight did not yet
    # observe the concurrent reviewer release. Use the real writer and transport.
    budget.set_override(home.conn, task, runs=10, cache_read_tokens=0, by="default",
                        reason="hermes kanban release: 10 more runs (mandate comment 123)",
                        writer_kind="agent", seen=seen)
    home.round()
    if lane == "review":
        result = home.hub.ops(board.BOARD, [{"client_op_id": board.uid(), "op": "request_review", "task": task}])
        assert result["results"][0]["status"] == "applied", result
    pool.round()
    pool.reopen()
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:agent-renewal") is not None, sync.pop_claim_deferral(task)
    after = pool.hub.reviewer_allowance(board.BOARD, task)["allowance"]
    assert after["review_uid"] == before["review_uid"]
    assert after["budget_event_uid"] == before["budget_event_uid"]
    assert after["used"] == before["used"] + 1
    assert after["remaining"] == before["remaining"] - 1
