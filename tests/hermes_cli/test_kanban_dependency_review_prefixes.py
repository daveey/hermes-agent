"""Mixed prefix reviews hand findings back without granting approval."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dependency_review as review
from hermes_cli import kanban_dependency_review_notice as notice
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board


def mixed_review():
    return {
        "reviewed_pr": helpers.PR,
        "reviewed_sha_prefix1": helpers.HEAD,
        "reviewed_sha_prefix2": "b" * 40,
        "verdicts": {"prefix1": "APPROVE", "prefix2": "REQUEST_CHANGES"},
    }


def test_mixed_prefix_review_resumes_once(board):
    task, parents = helpers.parked(board)
    helpers._finish(board, parents, mixed_review(), "VERDICT: REQUEST_CHANGES for prefix 2")
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("updates", [
    {"reviewed_sha_prefix1": "a" * 39},
    {"reviewed_sha_prefix2": "g" * 40},
    {"reviewed_sha_prefix2": []},
    {"reviewed_sha_prefix2": "b" * 40 + "\n"},
    {"reviewed_sha_prefix0": "a" * 40,
     "verdicts": {"prefix0": "APPROVE", "prefix1": "APPROVE", "prefix2": "REQUEST_CHANGES"}},
    {"verdicts": []},
    {"verdicts": ["prefix1", "prefix2"]},
    {"verdicts": {"prefix1": "APPROVE"}},
    {"verdicts": {"prefix1": "APPROVE", "prefix2": "APPROVE"}},
    {"verdicts": {"prefix1": [], "prefix2": "REQUEST_CHANGES"}},
    {"verdicts": {"prefix1": "unknown", "prefix2": "REQUEST_CHANGES"}},
    {"verdict": "APPROVE"},
    {"verdict": "REQUEST_CHANGES"},
    {"reviewed_pr": None},
    {"reviewed_pr": helpers.PR + "1"},
])
def test_incomplete_or_unbound_prefix_review_stays_held(board, updates):
    task, parents = helpers.parked(board)
    data = mixed_review() | updates
    helpers.finish_reviews(board, parents, metadata=data)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_prefix_review_is_not_approval_or_ordering_handoff(board):
    task, parents = helpers.parked(board)
    data = mixed_review()
    helpers.finish_reviews(board, parents, metadata=data)
    assert review.is_review_result(data, "Review complete", {helpers.PR})
    assert not review._exact_head(data)
    assert dispatch.check_respawn_guard(board, task) is None
    diagnostic = notice.finished_review_hold(board, task)
    assert "exact_head refused" not in diagnostic


@pytest.mark.parametrize("authenticated", [False, True])
def test_prefix_review_retains_checkpoint_scope(board, authenticated):
    task, parents = helpers.parked(board, authenticated=authenticated)
    helpers._finish(board, parents, mixed_review(), "VERDICT: REQUEST_CHANGES")
    if authenticated:
        helpers.worker_comment(board, task, "dev", helpers.PR + "1")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_unrelated_verdicts_do_not_mark_prerequisite_as_review():
    # Live implementation/research handoffs also carry plural verdicts.
    assert not review.is_review_result({
        "verdicts": {"benchmark": "PASS"}, "merge_sha": helpers.HEAD,
        "tests_run": 10, "pr": helpers.PR,
    }, "Implementation and benchmark complete", {helpers.PR})


def test_no_prefix_heads_is_not_repair():
    assert not review._prefix_repair_head({
        "reviewed_pr": helpers.PR, "verdicts": {"prefix1": "REQUEST_CHANGES"},
    })


@pytest.mark.parametrize("data", [
    {"reviewed_pr": 10, "reviewed_sha_prefix1": "abc"},
    {"reviewed_pr": 10, "reviewed_sha_prefix1": helpers.HEAD},
    {"reviewed_pr": 10, "reviewed_sha_prefix1": helpers.HEAD,
     "verdict_prefix1": "APPROVE"},
])
def test_prefix_hash_without_verdicts_cannot_use_ordering(board, data):
    task, parents = helpers.parked(board)
    helpers.finish_reviews(board, parents, metadata=data)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_integer_pr_mixed_review_uses_repair_not_approval(board):
    task, parents = helpers.parked(board)
    helpers.finish_reviews(board, parents, metadata=mixed_review() | {"reviewed_pr": 10})
    owner = board.execute("SELECT * FROM task_runs WHERE task_id=?", (task,)).fetchone()
    assert review._reviewed_prs(board, parents[0], 0, owner["id"], {helpers.PR}) == {helpers.PR}
    assert not review._approval_prs(board, parents[0], 0, owner["id"], {helpers.PR})
    assert dispatch.check_respawn_guard(board, task) is None
