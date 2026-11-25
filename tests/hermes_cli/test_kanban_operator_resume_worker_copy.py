"""An operator resume written on the home gateway releases the hold on a worker's kept copy. [daveey]

2026-11-12 (t_01a1cfdda6902e90): ``hermes kanban resume`` on the hub at 12:20:07 reached the Board in 1 s, yet
worker-3/4/6 kept recording ``respawn_guarded active_pr`` for 14 min. Cause: none of the six workers ran a core
with the resume step yet (version skew during the worker rollout), not the feed or the receipt. These tests pin
the path that incident never exercised on current code: real gateway stores, pickup and sync, the real guard.
The worker's guard-held copy stays (``kanban_worker_node._guard_held``), the feed brings it the receipt, and the
next tick runs it. The receipt names the run by uid, so the copy's remapped integer run ids do not matter.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_operator_resume as resume
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli.kanban_guard_helpers import worker_comment

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]
PR = "https://github.com/daveey/hermes/pull/9003"


@pytest.fixture
def clock(monkeypatch):
    now = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    for var in resume._WORKER_ENV + ("HERMES_KANBAN_RUN_ID",):
        monkeypatch.delenv(var, raising=False)
    return now


@pytest.fixture
def imports(monkeypatch):
    seen: list[str] = []
    real = wn.import_cards

    def counting(conn, replica, ids):
        done = real(conn, replica, ids)
        seen.extend(done)
        return done
    monkeypatch.setattr(wn, "import_cards", counting)
    return seen


def _status(conn, tid):
    task = kb.get_task(conn, tid)
    return task.status if task else None


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def _crash_after_pr(worker, tid, clock):
    """The worker's run publishes a PR and dies: ready again, no handoff."""
    worker.enter()
    worker_comment(worker.conn, tid, "default", f"Opened {PR}")
    clock[0] += 60
    assert kb.reclaim_task(worker.conn, tid, reason="worker crashed", signal_fn=lambda *_: None)
    assert _status(worker.conn, tid) == "ready"


def _held_on_worker(gws, clock):
    """A hub card the worker ran, crashed on after its PR, and now holds as a kept ``active_pr`` copy."""
    hub, worker = gws
    tid = support._hub_card(hub, title="owner of a PR", runs_on=PIN)
    worker.round()
    worker.tick()
    assert _status(worker.conn, tid) == "running"
    _crash_after_pr(worker, tid, clock)
    worker.round()  # push the comment and the crash
    worker.round()  # pull them back into the replica the next pickup imports from
    clock[0] += 60
    worker.tick()  # the guard holds it and records the hold
    assert _status(worker.conn, tid) == "ready" and "respawn_guarded" in _kinds(worker.conn, tid)
    worker.round()
    hub.round()
    worker.enter()
    assert wn.evict(worker.conn) == []  # kept: the local dispatcher holds it
    return tid


def test_home_resume_releases_the_workers_kept_copy_at_its_next_tick(isolate_usage_accounting, gws, clock, imports):
    hub, worker = gws
    tid = _held_on_worker(gws, clock)
    copied = imports.count(tid)
    hub.enter()
    assert kbd.check_respawn_guard(hub.conn, tid) == "active_pr"
    clock[0] += 60
    payload = resume.resume(hub.conn, tid, reason=f"review elsewhere approved {PR}", actor="default")
    assert payload["after_run"].get("run_uid") and payload["merge_authorized"] is False
    # Before the receipt syncs the worker still holds.
    worker.tick()
    assert _status(worker.conn, tid) == "ready"
    hub.round()
    worker.round()
    worker.enter()
    assert resume.EVENT in _kinds(worker.conn, tid)  # the feed carried it to the kept copy
    assert imports.count(tid) == copied  # the same copy, not a re-import
    assert kbd.check_respawn_guard(worker.conn, tid) is None
    clock[0] += 5
    worker.tick()
    assert _status(worker.conn, tid) == "running"
    # One continuation: the resumed run crashes too, and the receipt (it named the earlier run) is spent.
    clock[0] += 60
    assert kb.reclaim_task(worker.conn, tid, reason="crashed again", signal_fn=lambda *_: None)
    clock[0] += 60
    assert kbd.check_respawn_guard(worker.conn, tid) == "active_pr"
    worker.tick()
    assert _status(worker.conn, tid) == "ready"


def test_home_resume_accepts_worker_only_observation(isolate_usage_accounting, gws, clock, monkeypatch):
    """Real synced worker observation qualifies even when home's guard releases."""
    hub, worker = gws
    tid = _held_on_worker(gws, clock)
    hub.enter()
    clock[0] += 60
    with monkeypatch.context() as patch:
        patch.setattr(kbd, "check_respawn_guard", lambda *_: None)
        resume.resume(hub.conn, tid, reason="worker-only hold", actor="operator")
    hub.round()
    worker.round()
    worker.tick()
    assert _status(worker.conn, tid) == "running"


def test_home_resume_accepts_a_worker_hold_recorded_after_a_reassign(isolate_usage_accounting, gws, clock, monkeypatch):
    """t_01a1d120e7d7c686, 2026-11-12: a same-profile reassign on the hub, the workers re-held the card 3 s later,
    and hub ``resume`` refused with "guard: none". The hub's DB holds only the replicated worker events."""
    hub, worker = gws
    tid = _held_on_worker(gws, clock)
    hub.enter()
    clock[0] += 30
    assert kb.assign_task(hub.conn, tid, "default")
    hub.round()
    worker.round()
    clock[0] += 3
    worker.tick()  # the worker's own guard holds the copy again, after the reassign
    assert _status(worker.conn, tid) == "ready"
    worker.round()
    hub.round()
    hub.enter()
    held_after = [e for e in kb.list_events(hub.conn, tid) if e.kind in ("assigned", "respawn_guarded")]
    assert [e.kind for e in held_after][-2:] == ["assigned", "respawn_guarded"]  # replicated, not hub-written
    clock[0] += 30
    with monkeypatch.context() as patch:
        patch.setattr(kbd, "check_respawn_guard", lambda *_: None)
        resume.resume(hub.conn, tid, reason="runner-only hold after reassign", actor="operator")
    hub.round()
    worker.round()
    clock[0] += 5
    worker.tick()
    assert _status(worker.conn, tid) == "running"


def test_home_resume_does_not_settle_unknown_crash_usage(gws, clock):
    """An active-PR resume spends its own hold, not missing accounting evidence."""
    from hermes_cli import kanban_usage_accounting as accounting
    hub, worker = gws
    tid = _held_on_worker(gws, clock)
    hub.enter()
    clock[0] += 60
    resume.resume(hub.conn, tid, reason="isolated operator hold control", actor="default")
    hub.round()
    worker.round()
    worker.enter()
    assert kbd.check_respawn_guard(worker.conn, tid) is None
    assert accounting.pending(worker.conn, tid)
    clock[0] += 5
    worker.tick()
    assert _status(worker.conn, tid) == "ready"
    assert accounting.pending(worker.conn, tid)


def test_without_a_resume_the_kept_copy_stays_held(gws, clock):
    hub, worker = gws
    tid = _held_on_worker(gws, clock)
    for _ in range(2):
        clock[0] += 60
        hub.round()
        worker.round()
        worker.tick()
    assert _status(worker.conn, tid) == "ready"
    assert kbd.check_respawn_guard(worker.conn, tid) == "active_pr"


def test_home_resume_releases_a_fresh_import_by_run_uid(isolate_usage_accounting, gws, clock):
    """worker-1's path on 2026-11-12: no copy yet, the pickup imports the whole history and the guard releases."""
    hub, worker = gws
    tid = _held_on_worker(gws, clock)
    # Drop the kept copy as a machine that never held it would be: only the Board and the replica have the card.
    worker.conn.execute("UPDATE kanban_sync_state SET applying = 1")
    for table in ("task_events", "task_comments", "task_runs"):
        worker.conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (tid,))
    worker.conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    worker.conn.execute("UPDATE kanban_sync_state SET applying = 0")
    worker.conn.commit()
    hub.enter()
    clock[0] += 60
    resume.resume(hub.conn, tid, reason=f"review elsewhere approved {PR}", actor="default")
    hub.round()
    worker.round()
    clock[0] += 5
    worker.tick()
    assert _status(worker.conn, tid) == "running"
