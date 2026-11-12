"""A hermes-sync core-update requeue must not consume a PR owner's one-shot handoff. [daveey]

Production (2026-11-12, worker-5): the owner of PR #1271 parked on a re-review, the review
approved, the card was promoted and claimed, and 41 s later the core updater's restart-safe
requeue reclaimed that run. The dispatcher then held the card ``active_pr`` with no worker
until an operator intervened. That requeue is a deliberate pause, not a crash: the next
respawn resumes as if the requeued run never happened. A real crash keeps the hold.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_dependency_review as dr

board = dr.board
HEAD, PR, finish_reviews, parked = dr.HEAD, dr.PR, dr.finish_reviews, dr.parked

CRASH = "Worker died"


def _approved(board):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"verdict": "APPROVE", "head": HEAD, "pr": PR})
    kb.add_comment(board, task, "reviewer", "APPROVE at exact head " + HEAD + " " + PR)
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    return task


def _ordering(board):
    """An ordering (non-review) dependency: the parent's result is no review verdict."""
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"changed_files": ["a.py"]})
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    return task


def _reclaim(board, task, reason):
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=reason, signal_fn=lambda *_: None)
    assert kb.get_task(board, task).status == "ready"


@pytest.mark.parametrize("setup", [_approved, _ordering], ids=["review_checkpoint", "ordering"])
def test_core_update_requeue_keeps_the_handoff(board, setup):
    task = setup(board)
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    assert dispatch.check_respawn_guard(board, task) is None
    # Twice in a row (two drains) still resumes; the next real claim consumes it.
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("setup", [_approved, _ordering], ids=["review_checkpoint", "ordering"])
@pytest.mark.parametrize("reason", [CRASH, None, CORE_UPDATE_REQUEUE_REASON + " (by hand)"])
def test_crash_or_other_reclaim_still_consumes_the_handoff(board, setup, reason):
    task = setup(board)
    _reclaim(board, task, reason)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("setup", [_approved, _ordering], ids=["review_checkpoint", "ordering"])
def test_requeued_run_that_published_a_new_pr_stays_held(board, setup):
    """The requeue is neutral only for its own run: a PR it reported re-arms the guard."""
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = setup(board)
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, "dev", "Opened " + PR + "7")
    assert kb.reclaim_task(board, task, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_crash_event_on_requeued_run_is_not_neutral(board):
    task = _ordering(board)
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    run = board.execute("SELECT MAX(id) FROM task_runs WHERE task_id = ?", (task,)).fetchone()[0]
    with kb.write_txn(board):
        kb._append_event(board, task, "crashed", {"pid": 1}, run_id=run)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("reason,held", [(CORE_UPDATE_REQUEUE_REASON, None), (CRASH, "active_pr")],
                         ids=["core_update_requeue", "crash"])
def test_same_second_immediate_checkpoint_survives_requeue(board, monkeypatch, reason, held):
    """The same-second ``kanban_continue`` handoff (latest ended run) skips a requeue too."""
    import time

    from hermes_cli import kanban_continuation as continuation
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    task = kb.create_task(board, title="implementation", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    worker_comment(board, task, "dev", "Published " + PR)
    result = continuation.continue_task(board, task, checkpoint="PR open, next: CI", expected_run_id=run,
                                        resume_after_seconds=0)
    assert result["ok"], result
    assert dispatch.check_respawn_guard(board, task) is None
    _reclaim(board, task, reason)
    assert dispatch.check_respawn_guard(board, task) == held


@pytest.mark.parametrize("reason,respawns", [(CORE_UPDATE_REQUEUE_REASON, True), (CRASH, False)],
                         ids=["core_update_requeue", "crash"])
def test_dispatcher_respawns_after_core_update_requeue(board, monkeypatch, reason, respawns):
    """Incident sequence through the real dispatcher tick: park -> APPROVE -> claim -> reclaim."""
    spawned: list[str] = []
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: (lambda _name: True))
    task = _approved(board)
    _reclaim(board, task, reason)
    result = dispatch.dispatch_once(board, spawn_fn=lambda t, _ws, board=None: spawned.append(t.id))
    guarded = [tid for tid, _ in result.respawn_guarded]
    if respawns:
        assert spawned == [task] and task not in guarded
        assert not [e for e in kb.list_events(board, task) if e.kind == "respawn_guarded"]
    else:
        assert spawned == [] and (task, "active_pr") in result.respawn_guarded
