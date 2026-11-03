"""A supported operator reopen of a done card reaches a normal dispatch. [daveey]

``hermes kanban reopen`` (``kanban_db_operator.reopen_done_task``) moves a card that
completed moments ago back to ready with a ``reopened`` event. The respawn guard's
``recent_success`` step only treated ``status``/``promoted``/``unblocked``/``reclaimed`` as a
deliberate re-run, so the reopen sat held until the 1 h window elapsed (live: the Stage A
owner was reopened at 22:22:55 PST and guarded ``recent_success`` three seconds later).

A reopen strictly after the latest completed run now authorizes ONE rerun through the
normal dispatch path; a same-second or older reopen, a malformed ``reopened`` event and
unrelated events stay guarded; a live claim is not duplicated; open parents still gate;
a PR owner resumes its PR once and its next PR comment guards again; a fenced worker
shell still cannot reopen.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_db_operator as kbo
from tests.hermes_cli import test_kanban_edit_operator_fields as ops
from tests.hermes_cli import test_kanban_shared_blockers as shared
from tests.hermes_cli.kanban_guard_helpers import worker_comment

kanban_home = shared.kanban_home
spawns = shared.spawns

T0 = 1_793_686_000
PR = "https://github.com/daveey/hermes/pull/9001"


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


@pytest.fixture
def open_pr(monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")


def _ran_and_completed(conn, title="stage a", pr_comment=False):
    tid = kb.create_task(conn, title=title, assignee="default")
    assert kb.claim_task(conn, tid) is not None
    if pr_comment:
        worker_comment(conn, tid, "default", f"Opened {PR}")
    assert kb.complete_task(conn, tid, result="ok", summary="done")
    return tid


def _reopen(tid, reason="independent replay found defects"):
    assert ops._main(["reopen", tid, "--reason", reason]) == 0


def _guard(tid):
    with kbc.connect_closing() as conn:
        return kbd.check_respawn_guard(conn, tid)


def test_cli_reopen_after_success_dispatches_once(kanban_home, clock, spawns):
    spawned, spawn = spawns
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn)
    clock[0] += 30
    _reopen(tid)
    with kbc.connect_closing() as conn:
        first = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert (tid, "recent_success") not in first.respawn_guarded
        assert spawned == [tid]
        assert kb.get_task(conn, tid).status == "running"
        # The live claim is never duplicated by a second tick.
        clock[0] += 5
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [tid]
        assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL",
                            (tid,)).fetchone()[0] == 1


def test_dashboard_done_to_ready_still_dispatches(kanban_home, clock, spawns):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router

    spawned, spawn = spawns
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn)
    clock[0] += 30
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    r = TestClient(app).patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "ready"})
    assert r.status_code == 200, r.text
    with kbc.connect_closing() as conn:
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert not result.respawn_guarded and spawned == [tid]


def test_reopen_authorizes_one_rerun_only(kanban_home, clock, spawns):
    spawned, spawn = spawns
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn)
    clock[0] += 30
    _reopen(tid)
    with kbc.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [tid]
        clock[0] += 60
        assert kb.complete_task(conn, tid, result="corrected", summary="rerun done")
    # The rerun's completion is newer than the reopen: the older reopen no longer lifts.
    assert _guard(tid) == "recent_success"


def test_same_second_reopen_stays_guarded(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn)
    _reopen(tid)
    assert _guard(tid) == "recent_success"
    clock[0] += 1
    assert _guard(tid) == "recent_success"  # the tie is a fact of the event, not of the read time


@pytest.mark.parametrize("kind,payload", [
    ("edited", {"fields": ["body"]}),
    ("reprioritized", {"priority": 15}),
    ("model_override_set", {"model": "m", "provider": "p"}),
    ("review_reopened", {}),
    ("reopened", {}),
    ("reopened", {"previous_status": "blocked"}),
])
def test_unrelated_or_malformed_events_stay_guarded(kanban_home, clock, kind, payload):
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn)
        clock[0] += 30
        kb.add_comment(conn, tid, "default", "notes")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, payload)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    assert _guard(tid) == "recent_success"


def test_open_parent_still_gates_reopened_child(kanban_home, clock, spawns):
    spawned, spawn = spawns
    with kbc.connect_closing() as conn:
        child = _ran_and_completed(conn, title="child")
        parent = kb.create_task(conn, title="parent", assignee="other")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
    clock[0] += 30
    _reopen(child)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert child not in spawned
        assert kb.get_task(conn, child).status == "todo"
        clock[0] += 30
        assert kb.claim_task(conn, parent) is not None
        assert kb.complete_task(conn, parent, result="ok", summary="parent done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        kbd.dispatch_once(conn, spawn_fn=spawn)
    assert spawned.count(child) == 1


@pytest.mark.parametrize("reason", [f"review of {PR} found two defects", "review found two defects"])
def test_pr_owner_reopen_resumes_its_pr_once(kanban_home, clock, spawns, open_pr, reason):
    spawned, spawn = spawns
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn, pr_comment=True)
    clock[0] += 30
    _reopen(tid, reason)
    with kbc.connect_closing() as conn:
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert not result.respawn_guarded
        assert spawned == [tid]
        assert [t.id for t in kb.list_tasks(conn)] == [tid]  # same owner; no second implementation card
        clock[0] += 30
        worker_comment(conn, tid, "default", f"Pushed the corrections to {PR}")
    # The rerun's own PR report is newer than the reopen: a respawn is guarded again.
    assert _guard(tid) == "active_pr"


def test_reopen_without_newer_reopen_keeps_pr_guard(kanban_home, clock, open_pr):
    """No reopen: a done card's open PR still guards a ready row (the ordinary hold)."""
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn, pr_comment=True)
        clock[0] += 3700  # past the success window, inside the PR window
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    assert _guard(tid) == "active_pr"


def test_fenced_worker_shell_cannot_reopen(kanban_home, clock, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        tid = _ran_and_completed(conn)
        other = kb.create_task(conn, title="worker card", assignee="default")
    clock[0] += 30
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(kanban_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", other)
    assert ops._main(["reopen", tid, "--reason", "worker wants a rerun"]) != 0
    assert "cannot mutate" in capsys.readouterr().err
    # The durable boundary is the DB write itself, not only the CLI fast-fail.
    with kbc.connect_closing() as conn, pytest.raises(Exception, match="(?i)delegat|fenc"):
        kbo.reopen_done_task(conn, tid, reason="direct import", actor="worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "reopened"]
