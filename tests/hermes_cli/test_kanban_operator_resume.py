"""``hermes kanban resume``: operator-only release of an ``active_pr`` hold. [daveey]

A supported handoff for a READY owner the respawn guard holds (no move checkpoint,
no readable review result). It authorizes exactly one continuation of the latest
run, never a merge; worker lineage (the worker process, its tool shells, delegated
children) is refused and records nothing.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_operator_resume as resume
from tests.hermes_cli import test_kanban_edit_operator_fields as ops
from tests.hermes_cli import test_kanban_shared_blockers as shared
from tests.hermes_cli.kanban_guard_helpers import worker_comment

kanban_home = shared.kanban_home
spawns = shared.spawns

T0 = 1_793_686_000
PR = "https://github.com/daveey/hermes/pull/9002"


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    for var in ("HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    return now


def _held(conn):
    """A crash/reclaim after publishing a PR: no review, no handoff -> active_pr."""
    tid = kb.create_task(conn, title="implement", assignee="default")
    assert kb.claim_task(conn, tid) is not None
    worker_comment(conn, tid, "default", f"Opened {PR}")
    assert kb.reclaim_task(conn, tid, reason="worker crashed", signal_fn=lambda *_: None)
    assert kb.get_task(conn, tid).status == "ready"
    return tid


def _guard(tid):
    with kbc.connect_closing() as conn:
        return kbd.check_respawn_guard(conn, tid)


def _events(tid):
    with kbc.connect_closing() as conn:
        return [e for e in kb.list_events(conn, tid) if e.kind == resume.EVENT]


def test_crash_without_review_stays_guarded(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = _held(conn)
    clock[0] += 60
    assert _guard(tid) == "active_pr"


def test_operator_resume_releases_once_and_is_recorded(kanban_home, clock, spawns, capsys):
    spawned, spawn = spawns
    with kbc.connect_closing() as conn:
        tid = _held(conn)
    clock[0] += 60
    assert ops._main(["resume", tid, "--reason", f"review on another gateway asked for changes on {PR}"]) == 0
    assert "Resumed" in capsys.readouterr().out
    [event] = _events(tid)
    assert event.payload["via"] == "operator-resume" and event.payload["merge_authorized"] is False
    assert event.payload["after_run"] and event.payload["actor"]
    with kbc.connect_closing() as conn:
        assert any(c.body.startswith("RESUMED: ") for c in kb.list_comments(conn, tid))
    # The operator's note names the PR but is FYI: it must not re-arm the hold it records.
    assert _guard(tid) is None
    with kbc.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [tid]
        # The resumed run crashes too: the receipt named the previous run, so it is spent.
        clock[0] += 60
        assert kb.reclaim_task(conn, tid, reason="crashed again", signal_fn=lambda *_: None)
    assert _guard(tid) == "active_pr"


@pytest.mark.parametrize("var", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_worker_lineage_is_refused(kanban_home, clock, monkeypatch, capsys, var):
    with kbc.connect_closing() as conn:
        tid = _held(conn)
    clock[0] += 60
    monkeypatch.setenv(var, tid)
    assert ops._main(["resume", tid, "--reason", "let me run again"]) != 0
    assert "worker lineage" in capsys.readouterr().err
    with kbc.connect_closing() as conn, pytest.raises(resume.ResumeRefused, match="worker lineage"):
        resume.resume(conn, tid, reason="direct import", actor="worker")
    monkeypatch.delenv(var)
    assert _events(tid) == []
    assert _guard(tid) == "active_pr"


def test_resume_requires_reason_and_an_active_pr_hold(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = _held(conn)
        free = kb.create_task(conn, title="no PR", assignee="default")
        clock[0] += 60
        with pytest.raises(resume.ResumeRefused, match="reason"):
            resume.resume(conn, tid, reason="  ", actor="op")
        with pytest.raises(resume.ResumeRefused, match="active_pr"):
            resume.resume(conn, free, reason="no hold", actor="op")
    assert _events(tid) == [] and _events(free) == []


def test_same_second_or_forged_receipt_grants_nothing(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = _held(conn)
        # Same second as the PR comment: strictly-after ordering fails closed.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, resume.EVENT, {"via": "operator-resume", "scope": "one_continuation",
                                                       "merge_authorized": False, "after_run": None})
    assert _guard(tid) == "active_pr"
    clock[0] += 60
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, resume.EVENT, {"via": "operator-resume", "scope": "one_continuation",
                                                       "merge_authorized": True, "after_run": None})
    assert _guard(tid) == "active_pr"
