"""Dependency-review continuation must repair the same PR, not defeat respawn guards."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs

PR = "https://github.com/example/repo/pull/10"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def parked(conn, *, authenticated=True):
    task = kb.create_task(conn, title="implementation", assignee="dev")
    kb.claim_task(conn, task)
    run = kb.get_task(conn, task).current_run_id
    kb.add_comment(conn, task, "dev", PR)
    reviews = [kb.create_task(conn, title="review", assignee="reviewer") for _ in range(2)]
    for review in reviews:
        kb.link_tasks(conn, review, task, expected_child_run_id=run)
    assert kb.block_task(conn, task, kind="dependency", reason="Independent review",
                         expected_run_id=run if authenticated else None)
    return task, reviews


def finish_reviews(conn, reviews, *, verdict="changes_required", pr=PR):
    for review in reviews:
        kb.claim_task(conn, review)
        run = kb.get_task(conn, review).current_run_id
        assert kb.complete_task(conn, review, summary="Review complete", expected_run_id=run,
                                metadata={"verdict": verdict, "pr": pr, "reviewed_sha": "a" * 40})
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
    if mode != "pending":
        finish_reviews(board, reviews,
                       verdict="approved" if mode in {"approved", "noop_assign"} else "changes_required",
                       pr=PR + "1" if mode == "unrelated" else PR)
    if mode == "new_pr":
        kb.add_comment(board, task, "dev", PR + "1")
    if mode == "noop_assign":
        kb.assign_task(board, task, "dev")
    kb.add_comment(board, task, "reviewer", "changes_required " + PR)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
