"""Maintenance treats containment as a benign exclusion, not a failed attempt."""
from __future__ import annotations

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_service as fixtures

board = fixtures.board


def test_never_dispatched_descendant_is_not_decomposed_or_swept(board, monkeypatch):
    from hermes_cli import kanban_containment_service as service
    from hermes_cli import kanban_decompose as decompose
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    actor, authority, session = fixtures.arguments(board, monkeypatch)
    target = kb.create_task(board, title="Fixture relay", assignee="default", parents=[actor])
    consumer = kb.create_task(board, title="Fixture waiter", assignee="default", parents=[target])
    edges = [tuple(r) for r in board.execute("SELECT parent_id,child_id FROM task_links")]
    assert kb.get_task(board, target).status == "todo"
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (target,)).fetchone()[0] == 0
    with scoped_current_session_id(session):
        result = service.contain(board, task_id=target, authority=authority, summary="Fixture protected relay")
    assert result["terminal_for_run"] is False
    assert kb.get_task(board, actor).status == "running"
    assert "containment_pending" in kb.decomposition_refusal(board, target)
    assert target not in decompose.list_triage_ids()

    def forbidden(*args, **kwargs):
        pytest.fail("contained relay reached auxiliary model")

    monkeypatch.setattr(decompose, "_call_aux", forbidden)
    for force in (False, True):
        outcome = decompose.decompose_task(target, force=force)
        assert not outcome.ok and "containment_pending" in outcome.reason
    assert target not in sweep_stale_triage(board, now=10**11)
    kb.recompute_ready(board)
    assert kb.get_task(board, consumer).status == "todo"
    assert [tuple(r) for r in board.execute("SELECT parent_id,child_id FROM task_links")] == edges
    assert board.execute("SELECT consecutive_failures,human_request FROM tasks WHERE id=?", (target,)).fetchone()[:] == (0, None)
    assert kb.goal_run_status(board, actor) == "running"


@pytest.mark.parametrize("status", ["ready", "review", "todo", "triage"])
def test_task_marker_before_history_skips_every_dispatch_path(board, monkeypatch, status):
    from hermes_cli import kanban_db_dispatch_queries as queries
    from hermes_cli import kanban_db_sync as sync
    target = kb.create_task(board, title="Fixture prehistory marker", assignee="default")
    board.execute("UPDATE tasks SET status=?,containment_episode=? WHERE id=?", (status, uuid7(), target))
    snapshot = list(board.execute("SELECT kind,payload FROM task_events WHERE task_id=?", (target,)))

    def forbidden(*args, **kwargs):
        pytest.fail("held card attempted a Board lease")

    monkeypatch.setattr(sync, "claim_gate", forbidden)
    assert kb.claim_task(board, target) is None
    assert kb.claim_review_task(board, target) is None
    assert target not in {row["id"] for row in queries.lane_rows(board, status)}
    kb.recompute_ready(board)
    assert kb.get_task(board, target).status == status
    assert "containment_pending" in kb.decomposition_refusal(board, target)
    assert list(board.execute("SELECT kind,payload FROM task_events WHERE task_id=?", (target,))) == snapshot
