"""A Board grant returned unused must not spend a parked owner's review continuation.

The ordering-parent path already ignores such grants (test_kanban_dependency_unused_grant);
a review parent routes through ``resumes_review``, the guard notice and the owner-wake
hold release, which each looked at the card's newest run row.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_dependency_review as review
from hermes_cli import kanban_hold_release as hold
from hermes_cli.kanban_dependency_review_notice import finished_review_hold
from tests.hermes_cli import test_kanban_dependency_pr_handoff as helpers
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
