"""A two-PR checkpoint reviewed by one exact-head approval card per PR resumes once.

Production shape (t_01a1ceba11be1290): a core PR and a plugin PR, one review card each,
each approving only its own ``reviewed_pr``. The all-parents-approve-every-PR rule held
that owner forever, although every awaited parent approved and every PR was approved.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_dependency_review as helpers
from tests.hermes_cli.kanban_guard_helpers import worker_comment

board = helpers.board

CORE = "https://github.com/example/core/pull/10"
MODS = "https://github.com/example/mods/pull/60"


def parked_two_prs(conn):
    parked_at = int(time.time())
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(time, "time", lambda: parked_at - 1)
        task = kb.create_task(conn, title="implementation", assignee="dev")
        kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        worker_comment(conn, task, "dev", f"Core {CORE} and plugin {MODS}")
        clock.setattr(time, "time", lambda: parked_at)
        reviews = [kb.create_task(conn, title=f"review {n}", assignee="reviewer") for n in (1, 2)]
        for review in reviews:
            kb.link_tasks(conn, review, task, expected_child_run_id=run)
        assert kb.block_task(conn, task, kind="dependency", reason="Independent review",
                             expected_run_id=run)
    return task, reviews


def finish(conn, reviews, prs, verdicts=("approve", "approve")):
    for review, pr, verdict in zip(reviews, prs, verdicts):
        kb.claim_task(conn, review)
        run = kb.get_task(conn, review).current_run_id
        assert kb.complete_task(conn, review, summary="Review complete", expected_run_id=run,
                                metadata={"verdict": verdict, "reviewed_pr": pr,
                                          "reviewed_sha": helpers.HEAD})
    kb.recompute_ready(conn)


def test_split_per_pr_approvals_resume_once(board):
    task, reviews = parked_two_prs(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    finish(board, reviews, (CORE, MODS))
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    # One continuation only: the next claim consumes it, a crash does not grant another.
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("prs,verdicts", [
    ((CORE, CORE), ("approve", "approve")),  # nobody approved the plugin PR
    ((CORE, MODS + "1"), ("approve", "approve")),  # second approval names an unrelated PR
    ((CORE, MODS), ("approve", "unknown-but-not-approval")),  # sibling did not approve
    # The first approval covers the whole checkpoint, but the sibling still never approved.
    ((None, MODS), ("approve", "unknown-but-not-approval")),
])
def test_split_approvals_must_cover_every_pr_and_every_parent(board, prs, verdicts):
    task, reviews = parked_two_prs(board)
    data = [{"verdict": v, "reviewed_sha": helpers.HEAD} | ({"reviewed_pr": p} if p else {})
            for p, v in zip(prs, verdicts)]
    for review, metadata in zip(reviews, data):
        kb.claim_task(board, review)
        run = kb.get_task(board, review).current_run_id
        # An unknown verdict is repair evidence for its own PR only; drop the exact head so
        # it cannot resume through the repair path and the approval rule alone decides.
        if metadata["verdict"] != "approve":
            metadata = {k: v for k, v in metadata.items() if k != "reviewed_sha"}
        assert kb.complete_task(board, review, summary="Review complete", expected_run_id=run,
                                metadata=metadata)
    kb.recompute_ready(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
