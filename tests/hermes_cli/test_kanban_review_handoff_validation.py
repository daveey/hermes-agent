"""Validate review routing before ending the implementer's run."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli import test_kanban_review_model as helpers

board = helpers.board


@pytest.mark.parametrize("provider", ["auto", None])
def test_class_provider_roundtrip(board, provider):
    cfg = helpers.config(board)
    with kbc.connect() as conn:
        task = helpers.task(conn)
        kb.claim_task(conn, task.id)
        metadata = {"review_model": "class:deep", "review_provider": provider}
        assert kb.request_review(conn, task.id, metadata=metadata)
        assert tier.resolve_model_tier(conn, task, lane="review", cfg=cfg) == (
            helpers.OPUS, "anthropic", "review_override")
        assert kb.get_task(conn, task.id).model_override == helpers.SONNET


def test_historical_board_default_handoff(board):
    cfg = helpers.config(board)
    with kbc.connect() as conn:
        task = helpers.task(conn)
        kb.claim_task(conn, task.id)
        assert kb.request_review(conn, task.id)
        # Replay already-persisted legacy metadata, independent of ingress normalization.
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (
            json.dumps({"review_model": "class:deep", "review_provider": "board-default"}), task.id))
        assert tier.resolve_model_tier(conn, task, lane="review", cfg=cfg) == (
            helpers.OPUS, "anthropic", "review_override")


@pytest.mark.parametrize("metadata", [
    {"review_model": "class:deep", "review_provider": "anthropic"},
    {"review_model": "class:deep", "review_provider": "unknown-provider"},
    {"review_model": "class:deep", "review_provider": "board-default"},
    {"review_model": helpers.OPUS},
    {"review_model": ["class:deep"]},

    {"review_model": helpers.OPUS, "review_provider": []},
])
def test_invalid_handoff_refused_without_transition(board, metadata):
    helpers.config(board)
    with kbc.connect() as conn:
        task = helpers.task(conn)
        kb.claim_task(conn, task.id)
        before = kb.get_task(conn, task.id)
        events = kb.list_events(conn, task.id)
        with pytest.raises((ValueError, AttributeError)):
            kb.request_review(conn, task.id, metadata=metadata, with_reason=True)
        after = kb.get_task(conn, task.id)
        assert (after.status, after.current_run_id, after.claim_lock) == (
            before.status, before.current_run_id, before.claim_lock)
        assert kb.list_events(conn, task.id) == events
        assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (
            before.current_run_id,)).fetchone()[0] is None
