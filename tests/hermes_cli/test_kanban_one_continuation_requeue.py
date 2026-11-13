"""Every one-continuation grant survives a hermes-sync core-update requeue. [daveey]

Three paths grant an ``active_pr``-held card one continuation: the dependency-park handoff
(a review verdict), the owner-wake hold release, and ``hermes kanban resume``. Each one
re-implements "has the continuation been used?" against ``task_runs``, and the requeue rule
(:mod:`kanban_core_update_requeue`: a requeue is a pause) was added one path at a time; the
operator resume shipped without it (MacBook board, t_01a1d560c6d3bc3c, 2026-11-13). This file
runs the same grant -> claim -> requeue fixture through every path, so a new path that checks
the raw latest run fails here. Add a grant below for any new one-continuation path.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_dependency_review as dr
from tests.tools import test_kanban_owner_wake_hold_release as hold
from tests.tools import test_kanban_owner_wake_requeue as wake

board = wake.board


def _later(monkeypatch):
    """Pin the clock just past publication: receipts must be strictly after the PR comment."""
    now = int(time.time()) + 2
    monkeypatch.setattr(time, "time", lambda: now)


def _handoff(conn, monkeypatch):
    task, reviews = dr.parked(conn)
    dr.finish_reviews(conn, reviews, metadata={"verdict": "APPROVE", "head": dr.HEAD, "pr": dr.PR})
    kb.add_comment(conn, task, "reviewer", "APPROVE at exact head " + dr.HEAD + " " + dr.PR)
    return task


def _hold_release(conn, monkeypatch):
    task, _, _ = hold.held(conn, monkeypatch)
    _later(monkeypatch)
    assert wake._unblock(task).get("ok") is True
    return task


def _operator_resume(conn, monkeypatch):
    from hermes_cli import kanban_operator_resume as resume
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = kb.create_task(conn, title="implement", assignee="dev")
    assert kb.claim_task(conn, task) is not None
    worker_comment(conn, task, "dev", "Opened " + dr.PR)
    assert kb.reclaim_task(conn, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"
    _later(monkeypatch)
    resume.resume(conn, task, reason="operator: continue the PR", actor="op")
    return task


def _cycle(conn, task, reason):
    assert kb.claim_task(conn, task) is not None
    assert kb.reclaim_task(conn, task, reason=reason, signal_fn=lambda *_: None)
    assert kb.get_task(conn, task).status == "ready"
    return dispatch.check_respawn_guard(conn, task)


GRANTS = [_handoff, _hold_release, _operator_resume]
IDS = ["dependency_handoff", "owner_wake_hold_release", "operator_resume"]


@pytest.mark.parametrize("grant", GRANTS, ids=IDS)
def test_requeue_is_a_pause_and_the_next_real_claim_spends_the_grant(board, monkeypatch, grant):
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    with kbc.connect() as conn:
        task = grant(conn, monkeypatch)
        assert kb.get_task(conn, task).status == "ready"
        assert dispatch.check_respawn_guard(conn, task) is None
        assert _cycle(conn, task, CORE_UPDATE_REQUEUE_REASON) is None  # a drain mid-run: a pause
        assert _cycle(conn, task, CORE_UPDATE_REQUEUE_REASON) is None  # a second drain, still a pause
        assert _cycle(conn, task, "Worker died") == "active_pr"  # the real continuation spends it


@pytest.mark.parametrize("grant", GRANTS, ids=IDS)
@pytest.mark.parametrize("reason", ["Worker died", None, CORE_UPDATE_REQUEUE_REASON + " (by hand)"],
                         ids=["crash", "no_reason", "near_miss_reason"])
def test_any_other_reclaim_spends_the_grant(board, monkeypatch, grant, reason):
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    with kbc.connect() as conn:
        task = grant(conn, monkeypatch)
        assert _cycle(conn, task, reason) == "active_pr"
