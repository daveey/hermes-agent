"""A park awaiting an exact-head review AND an ordering prerequisite resumes once.

Production shape (hub t_01a1de3b070ed3b8, PR #3295): the owner parked on an exact-head
approval card and on a base-CI repair card whose result carries no verdict. The ordering
handoff refused the review parent and the review rules demanded that the repair card
approve too, so the owner was held ``active_pr`` until the PR it alone could merge closed.
A done ordering prerequisite neither authorizes nor refuses; the review parents decide.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_move as km
from hermes_cli.kanban_dependency_review import resumes_review
from tests.hermes_cli import test_kanban_dependency_review as helpers
from tests.hermes_cli import test_kanban_move as moves
from tests.hermes_cli.kanban_guard_helpers import worker_comment

board = helpers.board
svc = moves.svc
gws = moves.gws
PR, HEAD = helpers.PR, helpers.HEAD
APPROVE = {"verdict": "approve", "reviewed_pr": PR, "reviewed_sha": HEAD}
# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x.
HR = "the card's worktree is on that host"
# The live repair card named only its own superseding PR, never the held one.
ORDERING = {"pr": "https://github.com/example/repo/pull/11"}


def finish(conn, parent, metadata, summary="Review complete"):
    kb.claim_task(conn, parent)
    run = kb.get_task(conn, parent).current_run_id
    assert kb.complete_task(conn, parent, summary=summary, expected_run_id=run, metadata=metadata)
    kb.recompute_ready(conn)


def parked_mixed(conn, review=APPROVE, ordering=ORDERING):
    task, (review_card, ordering_card) = helpers.parked(conn)
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"
    if review is not None:
        finish(conn, review_card, review)
    if ordering is not None:
        finish(conn, ordering_card, ordering, summary="Base CI repaired; superseded by PR #11")
    return task


def test_exact_head_approval_plus_done_ordering_parent_resumes_once(board):
    task = parked_mixed(board)
    assert kb.get_task(board, task).status == "ready"
    assert resumes_review(board, task, [PR]) is True
    assert dispatch.check_respawn_guard(board, task) is None
    # One continuation only: the next claim consumes it, a crash does not grant another.
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_ordering_parent_not_done_still_holds(board):
    task = parked_mixed(board, ordering=None)
    assert resumes_review(board, task, [PR]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("review", [
    {"verdict": "approve", "reviewed_pr": PR, "head": "0d0f9d77"},  # no exact head
    {"verdict": "approve", "reviewed_pr": PR + "1", "reviewed_sha": HEAD},  # unrelated PR
    {"pr": PR},  # a review whose verdict was lost: never an ordering prerequisite
])
def test_review_parent_without_valid_approval_still_holds(board, review):
    task = parked_mixed(board, review=review)
    assert resumes_review(board, task, [PR]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_exact_head_change_request_plus_ordering_parent_hands_back_for_repair(board):
    task = parked_mixed(board, review={"verdict": "REQUEST_CHANGES", "reviewed_pr": PR, "reviewed_sha": HEAD})
    assert dispatch.check_respawn_guard(board, task) is None


def test_legacy_checkpoint_without_awaited_list_keeps_per_parent_rule(board):
    # Only parents the checkpoint named as awaited can be ordering prerequisites.
    task, (review_card, ordering_card) = helpers.parked(board)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? "
                        "AND kind = 'dependency_wait'", (task,)).fetchone()
    payload = json.loads(row["payload"])
    payload["review_checkpoint"].pop("awaited", None)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    finish(board, review_card, APPROVE)
    finish(board, ordering_card, ORDERING, summary="Base CI repaired; superseded by PR #11")
    assert resumes_review(board, task, [PR]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_only_ordering_parents_keep_the_ordering_handoff(board):
    task, parents = helpers.parked(board)
    for parent in parents:
        finish(board, parent, ORDERING, summary="prerequisite finished")
    # Unchanged: the review rules never claim an all-ordering park; the ordering handoff does.
    assert resumes_review(board, task, [PR]) is False
    assert dispatch.check_respawn_guard(board, task) is None


def test_foreign_review_and_ordering_parents_resume_on_the_importing_worker(gws, monkeypatch):
    """Hub case: both parents live on another gateway; only their mirrored results are local."""
    hub, worker = gws
    hub.enter()
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(hub.conn, title="finish existing PR", assignee="dev")
    kb.claim_task(hub.conn, task)
    run = kb.get_task(hub.conn, task).current_run_id
    worker_comment(hub.conn, task, "dev", PR)
    review, ordering = (kb.create_task(hub.conn, title=t, assignee="dev") for t in ("review", "base repair"))
    for parent in (review, ordering):
        kb.link_tasks(hub.conn, parent, task, expected_child_run_id=run)
    clock[0] += 10
    assert kb.block_task(hub.conn, task, kind="dependency", reason="review and base repair", expected_run_id=run)
    clock[0] += 10
    finish(hub.conn, review, APPROVE)
    finish(hub.conn, ordering, ORDERING, summary="Base CI repaired; superseded by PR #11")
    clock[0] += 1
    assert km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR,
                          reason="continue on worker", checkpoint="")["ok"]
    hub.round()
    worker.round()
    assert kb.get_task(worker.conn, review) is None and kb.get_task(worker.conn, ordering) is None
    tick = worker.tick()
    assert (task, "active_pr") not in tick.respawn_guarded
    assert kb.get_task(worker.conn, task).status == "running", vars(tick)
    assert prs.active_pr_reason(worker.conn, task, int(time.time())) == "active_pr"
