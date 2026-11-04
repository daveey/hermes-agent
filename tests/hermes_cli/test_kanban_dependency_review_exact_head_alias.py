"""Exact-head review metadata accepts the producer's exact_head spelling."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board


@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES", "observations"])
def test_exact_head_alias_resumes_once(board, verdict):
    task, parents = helpers.parked(board)
    helpers.finish_reviews(board, parents, metadata={
        "reviewed_pr": helpers.PR, "exact_head": helpers.HEAD, "verdict": verdict,
    })
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("value", [None, 123, [], "", "a" * 39, "a" * 41, "g" * 40, "a" * 40 + "\n"])
def test_malformed_alias_is_not_exact(board, value):
    task, parents = helpers.parked(board)
    helpers.finish_reviews(board, parents, metadata={
        "reviewed_pr": helpers.PR, "exact_head": value, "verdict": "APPROVE",
    })
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_alias_does_not_cover_another_pr(board):
    task, parents = helpers.parked(board)
    helpers.finish_reviews(board, parents, metadata={
        "reviewed_pr": helpers.PR + "1", "exact_head": helpers.HEAD, "verdict": "APPROVE",
    })
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
