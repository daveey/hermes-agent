"""Dependency-review continuation must repair the same PR, not defeat respawn guards."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.kanban_guard_helpers import worker_comment

PR = "https://github.com/example/repo/pull/10"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    from hermes_cli.kanban_model_assignment import _assignee_config
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    assert _assignee_config("dev") is None
    profile = tmp_path / "profiles" / "dev"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(_managed_yaml())
    config = _assignee_config("dev")
    assert config is not None and config["model_routing"]["enabled"]
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def parked(conn, *, authenticated=True, park_delay=1):
    """Owner publishes its PR, then parks ``park_delay`` whole seconds later.

    The clock is pinned, not wall-clock: the ordering-dependency handoff only
    considers a park strictly after the PR comment, so an unpinned fixture
    flipped between that path and the same-second tie with runner load. 1 s is
    production's usual ordering; both stamps are at or before real "now", so
    later events (review completion, promotion) still sort after the park.
    """
    parked_at = int(time.time())
    published = parked_at - park_delay
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(time, "time", lambda: published)
        task = kb.create_task(conn, title="implementation", assignee="dev")
        kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        worker_comment(conn, task, "dev", PR)
        clock.setattr(time, "time", lambda: parked_at)
        reviews = [kb.create_task(conn, title="review", assignee="reviewer") for _ in range(2)]
        for review in reviews:
            kb.link_tasks(conn, review, task, expected_child_run_id=run)
        assert kb.block_task(conn, task, kind="dependency", reason="Independent review",
                             expected_run_id=run if authenticated else None)
    return task, reviews


HEAD = "0d0f9d77a5e5bbb455499b0f4b69bf20201c24c7"


def finish_reviews(conn, reviews, *, verdict="changes_required", pr=PR, metadata=None):
    for review in reviews:
        kb.claim_task(conn, review)
        run = kb.get_task(conn, review).current_run_id
        body = {"verdict": verdict, "pr": pr, "reviewed_sha": "a" * 40} if metadata is None else metadata
        assert kb.complete_task(conn, review, summary="Review complete", expected_run_id=run,
                                metadata=body)
    kb.recompute_ready(conn)


def test_completed_reviews_resume_same_pr_despite_later_comments(board):
    task, reviews = parked(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    # Production ordering: owner parks; reviews comment with PR URL; guard notice;
    # review completions promote; another reviewer repeats the SAME PR.
    kb.add_comment(board, task, "reviewer", "Repair " + PR)
    kb.add_comment(board, task, "dispatcher", "active_pr hold: " + PR)
    finish_reviews(board, reviews)
    kb.add_comment(board, task, "reviewer", "Changes required at " + PR)
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    # Permission is consumed by the next claim, even if that worker crashes.
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("mode", ["unowned", "unrelated", "approved", "new_pr", "noop_assign", "pending"])
def test_dependency_and_comments_do_not_grant_generic_retry(board, mode):
    task, reviews = parked(board, authenticated=mode != "unowned")
    if mode in {"approved", "noop_assign"}:
        # Approval without a full head is not an exact-head handoff.
        finish_reviews(board, reviews, metadata={"verdict": "approved", "pr": PR, "head": "0d0f9d77"})
    elif mode != "pending":
        finish_reviews(board, reviews,
                       verdict="changes_required",
                       pr=PR + "1" if mode == "unrelated" else PR)
    if mode == "new_pr":
        worker_comment(board, task, "dev", PR + "1")
    if mode == "noop_assign":
        kb.assign_task(board, task, "dev")
    kb.add_comment(board, task, "reviewer", "changes_required " + PR)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("metadata", [
    {"verdict": "APPROVE", "head": HEAD},
    {"verdict": "approved", "reviewed_sha": HEAD, "pr": PR},
])
def test_exact_head_approval_releases_once_despite_later_same_pr_comment(board, metadata):
    """Production sequence: checkpoint records the PR, review completes at an
    exact head, then a reviewer comment repeats that PR. Held before, released
    after, and a crash/reclaim does not get a second continuation."""
    task, reviews = parked(board)
    kb.add_comment(board, task, "reviewer", "Reviewing " + PR)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    task_row = kb.get_task(board, task)
    assert task_row is not None and task_row.status != "done"
    finish_reviews(board, reviews, metadata=metadata)
    kb.add_comment(board, task, "reviewer", "APPROVE at exact head " + HEAD + " " + PR)
    task_row = kb.get_task(board, task)
    assert task_row is not None and task_row.status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_exact_head_approval_of_unrelated_pr_stays_held(board):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={
        "verdict": "APPROVE", "head": HEAD, "pr": PR + "1",
    })
    kb.add_comment(board, task, "reviewer", "Approved a different head " + PR)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_new_pr_after_exact_head_approval_stays_held(board):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"verdict": "APPROVE", "head": HEAD})
    worker_comment(board, task, "dev", "Opened a new head " + PR + "9")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


SHA = "c" * 40


def _finish(board, reviews, metadata, summary):
    for review in reviews:
        kb.claim_task(board, review)
        run = kb.get_task(board, review).current_run_id
        assert kb.complete_task(board, review, summary=summary, expected_run_id=run, metadata=metadata)
    kb.recompute_ready(board)


@pytest.mark.parametrize("metadata,summary", [
    ({"verdict": "REQUEST_CHANGES", "reviewed_sha": SHA, "approved": False}, "VERDICT: REQUEST_CHANGES on PR #10"),
    ({"verdict": "request-changes", "reviewed_sha": SHA, "pr": PR}, "Review complete"),
    ({"approved": False, "reviewed_sha": SHA}, "VERDICT: REQUEST_CHANGES on PR #10 at " + SHA),
])
def test_review_request_changes_hands_pr_back_to_author(board, metadata, summary):
    """Production shape: reviewers write REQUEST_CHANGES, not changes_required, and no ``pr``.
    The author's own later PR comment must not re-arm the hold; one continuation only."""
    task, reviews = parked(board)
    _finish(board, reviews, metadata, summary)
    worker_comment(board, task, "dev", "Waiting on review of " + PR)
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("metadata,summary", [
    ({"verdict": "REQUEST_CHANGES", "reviewed_sha": SHA, "pr": PR + "1"}, "x"),
    ({"verdict": "REQUEST_CHANGES", "reviewed_sha": SHA}, "VERDICT: REQUEST_CHANGES on PR #99"),
    ({"verdict": "REQUEST_CHANGES"}, "no exact head"),
    ({"verdict": "approved", "reviewed_sha": "short"}, "VERDICT: REQUEST_CHANGES"),
    ({"approved": False}, "Looks fine, nothing about a verdict"),
])
def test_review_request_changes_for_other_pr_or_without_head_stays_held(board, metadata, summary):
    task, reviews = parked(board)
    _finish(board, reviews, metadata, summary)
    worker_comment(board, task, "dev", "Waiting on review of " + PR)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
