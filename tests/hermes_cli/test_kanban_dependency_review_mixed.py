"""A two-PR checkpoint whose per-PR reviews disagree (one approves, one requests changes) resumes once.

Production shape (t_01a1cedd5cab762d): a state PR1325 and a state PR1326, one exact-head
review card each. PR1326's review approved; PR1325's review requested changes. The repair
path needed one parent's change request to cover both PRs and the split-approval path needed
every parent to approve, so the owner was held by ``active_pr`` with no path back to repair
PR1325, though every awaited parent left exact-head evidence for its own PR.
"""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_dependency_review import resumes_review
from tests.hermes_cli import test_kanban_dependency_review as helpers
from tests.hermes_cli import test_kanban_dependency_review_split as split

board = helpers.board
CORE, MODS = split.CORE, split.MODS


def finish(conn, reviews, metadata):
    for review, data in zip(reviews, metadata):
        kb.claim_task(conn, review)
        run = kb.get_task(conn, review).current_run_id
        assert kb.complete_task(conn, review, summary="Review complete", expected_run_id=run, metadata=data)
    kb.recompute_ready(conn)


def changes(pr):
    return {"verdict": "request_changes", "reviewed_pr": pr, "reviewed_sha": helpers.HEAD}


def approve(pr):
    return {"verdict": "approve", "reviewed_pr": pr, "reviewed_sha": helpers.HEAD}


def test_changes_on_one_pr_and_approval_of_the_other_resume_once(board):
    task, reviews = split.parked_two_prs(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    finish(board, reviews, [changes(CORE), approve(MODS)])
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    # One continuation only: the next claim consumes it, a crash does not grant another.
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_mixed_reviews_must_still_cover_every_pr(board):
    # Change request and approval both name the core PR: nobody reviewed the plugin PR.
    task, reviews = split.parked_two_prs(board)
    finish(board, reviews, [changes(CORE), approve(CORE)])
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_mixed_reviews_need_exact_head_evidence_from_every_parent(board):
    # The plugin review has no exact head: it is neither an approval nor repair evidence.
    task, reviews = split.parked_two_prs(board)
    finish(board, reviews, [changes(CORE), {"verdict": "approve", "reviewed_pr": MODS, "head": "0d0f9d77"}])
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_mixed_approval_of_unrelated_pr_does_not_cover(board):
    task, reviews = split.parked_two_prs(board)
    finish(board, reviews, [changes(CORE), approve(MODS + "1")])
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_legacy_checkpoint_mixed_reviews_keep_per_parent_rule(board):
    # Without an explicit awaited list, a parent approving only one PR covers nothing on its own.
    task, reviews = split.parked_two_prs(board)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? "
                        "AND kind = 'dependency_wait'", (task,)).fetchone()
    payload = json.loads(row["payload"])
    payload["review_checkpoint"].pop("awaited", None)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    finish(board, reviews, [changes(CORE), approve(MODS)])
    assert resumes_review(board, task, [CORE, MODS]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
