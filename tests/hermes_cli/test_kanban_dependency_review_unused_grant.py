"""A Board grant returned unused must not spend a parked owner's review continuation.

The ordering-parent path already ignores such grants (test_kanban_dependency_unused_grant);
a review parent routes through ``resumes_review``, the guard notice and the owner-wake
hold release, which each looked at the card's newest run row. So did the same-second
``kanban_continue`` handoff and the respawn guard's cooldown reader (newest ended run).
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_dependency_review as review
from hermes_cli import kanban_hold_release as hold
from hermes_cli.kanban_core_update_requeue import (
    CORE_UPDATE_REQUEUE_REASON, latest_ended_run_skipping_requeues, latest_run_skipping_requeues)
from hermes_cli.kanban_dependency_review_notice import finished_review_hold
from tests.hermes_cli import test_kanban_dependency_pr_handoff as helpers
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli.test_kanban_dependency_unused_grant import release_unused

board = helpers.board
PR = helpers.PR
HEAD = "b" * 40


def parked_on_review(conn, monkeypatch, *, metadata=None):
    """Owner publishes its PR, parks on a review card, and the review finishes."""
    task, run, clock = helpers.park(conn, monkeypatch, finish=False)
    parent = kb.parent_ids(conn, task)[0]
    clock[0] += 10
    kb.claim_task(conn, parent)
    reviewer_run = kb.get_task(conn, parent).current_run_id
    body = {"verdict": "request_changes", "reviewed_pr": PR, "reviewed_sha": HEAD} if metadata is None else metadata
    assert kb.complete_task(conn, parent, summary="Review complete", expected_run_id=reviewer_run, metadata=body)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task).status == "ready"
    return task, run, clock


def test_fixture_resumes_without_a_grant(board, monkeypatch):
    task, _, _ = parked_on_review(board, monkeypatch)
    assert review.resumes_review(board, task, [PR])
    assert prs.active_pr_reason(board, task, int(time.time())) is None


def test_unused_grants_preserve_review_continuation(board, monkeypatch, all_assignees_spawnable):
    task, _, clock = parked_on_review(board, monkeypatch)
    release_unused(board, task, clock)
    release_unused(board, task, clock)
    assert review.resumes_review(board, task, [PR])
    assert prs.active_pr_reason(board, task, int(time.time())) is None
    launched = []
    result = dispatch.dispatch_once(board, spawn_fn=lambda task, workspace, **kwargs: launched.append(task.id) or 123456789)
    assert task in launched, result
    # The real claim consumes the one continuation.
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("evidence", ["worker_bound", "spawned", "crashed", "reclaimed", "timed_out", "heartbeat"])
def test_used_or_failed_grant_still_consumes_review_continuation(board, monkeypatch, evidence):
    task, _, clock = parked_on_review(board, monkeypatch)
    release_unused(board, task, clock, evidence=evidence)
    assert not review.resumes_review(board, task, [PR])
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_release_without_unused_receipt_still_consumes_review_continuation(board, monkeypatch):
    task, _, clock = parked_on_review(board, monkeypatch)
    release_unused(board, task, clock, unused=False)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_guard_notice_describes_the_park_behind_an_unused_grant(board, monkeypatch):
    # A short head is not exact, so the owner stays held; the notice must still name the
    # refused predicate instead of going silent because the newest run row is a bare grant.
    task, _, clock = parked_on_review(
        board, monkeypatch, metadata={"verdict": "request_changes", "reviewed_pr": PR, "reviewed_sha": "b59783b8"})
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"
    before = finished_review_hold(board, task)
    assert "exact_head refused" in before
    release_unused(board, task, clock)
    assert finished_review_hold(board, task) == before


def test_hold_release_scope_and_receipt_survive_an_unused_grant(board, monkeypatch):
    task, run, clock = parked_on_review(board, monkeypatch)
    release_unused(board, task, clock)
    row, captured, proofs = hold._scope(board, task)
    assert row["id"] == run and proofs
    clock[0] += 1
    with kb.write_txn(board):
        kb._append_event(board, task, "hold_released", {
            "via": "owner-wake-hold-release", "supervisor": "s", "supervisor_board": "b",
            "park": hold._identity(row), "prs": sorted(captured), "reviews": proofs,
            "scope": "one_continuation", "merge_authorized": False})
    clock[0] += 1
    published = clock[0] - 100
    assert hold.released_after(board, task, published, [PR])
    release_unused(board, task, clock)
    assert hold.released_after(board, task, published, [PR])
    # A started run consumes the receipt.
    release_unused(board, task, clock, evidence="spawned")
    assert not hold.released_after(board, task, published, [PR])


def test_hold_release_refuses_a_second_release_for_the_park_across_a_grant(board, monkeypatch):
    # The grant leaves the park the latest run, so its receipt still names it: no second release.
    task, _, clock = parked_on_review(board, monkeypatch)
    monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *a, **k: "active_pr")

    def release():
        hold.release(board, task, supervisor="s", supervisor_board="b", precondition=lambda _conn: None)

    clock[0] += 1
    release()
    release_unused(board, task, clock)
    clock[0] += 1
    with pytest.raises(ValueError, match="already used for this park"):
        release()
    release_unused(board, task, clock, evidence="spawned")
    with pytest.raises(ValueError, match="latest orderly owner park"):
        release()


def test_replica_card_skips_a_grant_by_its_own_run_identity(board, monkeypatch):
    """A replica holds the home's runs under new local ids; the park names its run by uid."""
    task, run, clock = parked_on_review(board, monkeypatch)
    release_unused(board, task, clock)
    release_unused(board, task, clock)
    with kb.write_txn(board):
        if not any(c[1] == "uid" for c in board.execute("PRAGMA table_info(task_runs)")):  # sync-only column
            board.execute("ALTER TABLE task_runs ADD COLUMN uid TEXT")
        board.execute("UPDATE task_runs SET uid = 'home-run-' || id WHERE task_id = ?", (task,))
        for table, column in (("task_events", "run_id"), ("task_runs", "id")):  # sync remaps them together
            board.execute(f"UPDATE {table} SET {column} = {column} + 1000 WHERE task_id = ? AND {column} IS NOT NULL",
                          (task,))
        event = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' "
                              "ORDER BY id DESC LIMIT 1", (task,)).fetchone()
        payload = json.loads(event["payload"])
        payload["review_checkpoint"].pop("run_id", None)
        payload["review_checkpoint"]["run_uid"] = f"home-run-{run}"
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), event["id"]))
    assert latest_run_skipping_requeues(board, task)["uid"] == f"home-run-{run}"
    assert review.resumes_review(board, task, [PR])
    assert prs.active_pr_reason(board, task, int(time.time())) is None
    assert hold._scope(board, task)[0]["uid"] == f"home-run-{run}"
    # A grant whose events did not travel with its run row is unattested and counts.
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET run_id = NULL WHERE task_id = ? AND run_id = "
                      "(SELECT MAX(id) FROM task_runs WHERE task_id = ?)", (task, task))
    assert not review.resumes_review(board, task, [PR])
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_pickers_return_none_when_only_unused_grants_ran(board, monkeypatch):
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="never started", assignee="dev")
    release_unused(board, task, clock)
    release_unused(board, task, clock)
    assert latest_run_skipping_requeues(board, task) is None
    assert latest_ended_run_skipping_requeues(board, task) is None
    assert latest_ended_run_skipping_requeues(board, task, by_ended_at=True) is None
    assert not review.resumes_review(board, task, [PR])
    assert not prs._immediate_checkpoint_after(board, task, clock[0] - 10, 0)
    assert dispatch.check_respawn_guard(board, task) is None
    with pytest.raises(ValueError, match="latest orderly owner park"):
        hold._scope(board, task)


def checkpointed(board, monkeypatch):
    """Claim, report the PR and ``kanban_continue`` in one second: the immediate-checkpoint handoff."""
    from hermes_cli import kanban_continuation as continuation

    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="implementation", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    worker_comment(board, task, "dev", "Published " + PR)
    result = continuation.continue_task(board, task, checkpoint="PR open, next: CI", expected_run_id=run,
                                        resume_after_seconds=0)
    assert result["ok"], result
    assert dispatch.check_respawn_guard(board, task) is None
    return task, clock


def test_unused_grant_keeps_the_same_second_checkpoint(board, monkeypatch):
    task, clock = checkpointed(board, monkeypatch)
    release_unused(board, task, clock)
    release_unused(board, task, clock)
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("evidence", ["spawned", "worker_bound", "crashed", "timed_out"])
def test_started_run_after_a_grant_consumes_the_checkpoint(board, monkeypatch, evidence):
    task, clock = checkpointed(board, monkeypatch)
    release_unused(board, task, clock)
    release_unused(board, task, clock, evidence=evidence)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


COOLDOWNS = [("spawn_failed", {"infrastructure": True}, "infrastructure_cooldown"),
             ("reclaimed", {"owner_restart": True}, "owner_restart_cooldown")]


def ended_run(board, task, clock, outcome, metadata):
    """A run the host closed with ``outcome`` (a refused spawn, a run lost to its owner's restart)."""
    clock[0] += 1
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    with kb.write_txn(board):
        board.execute("UPDATE task_runs SET status = ?, outcome = ?, ended_at = ?, claim_lock = NULL, metadata = ? "
                      "WHERE id = ?", (outcome, outcome, clock[0], json.dumps(metadata), run))
        board.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
                      "current_run_id = NULL WHERE id = ?", (task,))


def cooled(board, monkeypatch, outcome, metadata, reason):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "300")
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="flaky host", assignee="dev")
    ended_run(board, task, clock, outcome, metadata)
    assert dispatch.check_respawn_guard(board, task) == reason
    return task, clock


@pytest.mark.parametrize("outcome,metadata,reason", COOLDOWNS, ids=["infrastructure", "owner_restart"])
def test_unused_grant_does_not_end_a_cooldown_early(board, monkeypatch, outcome, metadata, reason):
    task, clock = cooled(board, monkeypatch, outcome, metadata, reason)
    release_unused(board, task, clock)
    release_unused(board, task, clock)
    assert dispatch.check_respawn_guard(board, task) == reason
    clock[0] += 300
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("outcome,metadata,reason", COOLDOWNS, ids=["infrastructure", "owner_restart"])
def test_core_update_requeue_does_not_end_a_cooldown_early(board, monkeypatch, outcome, metadata, reason):
    task, clock = cooled(board, monkeypatch, outcome, metadata, reason)
    clock[0] += 1
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == reason


@pytest.mark.parametrize("outcome,metadata,reason", COOLDOWNS, ids=["infrastructure", "owner_restart"])
def test_runs_after_a_grant_still_count_for_the_cooldown(board, monkeypatch, outcome, metadata, reason):
    task, clock = cooled(board, monkeypatch, outcome, metadata, reason)
    release_unused(board, task, clock)
    # A run that started supersedes the cooled-down failure ...
    release_unused(board, task, clock, evidence="spawned")
    assert dispatch.check_respawn_guard(board, task) != reason
    # ... and a failure after a grant starts a new cooldown.
    release_unused(board, task, clock)
    ended_run(board, task, clock, outcome, metadata)
    assert dispatch.check_respawn_guard(board, task) == reason
