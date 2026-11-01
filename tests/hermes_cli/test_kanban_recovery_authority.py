"""Recovery executes beside its target, not on a portable scratch worker.

Real stores, pickup, leases, eviction and sync; only Board transport/spawn are fakes.
"""
from __future__ import annotations

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_resume_exec as rx
from hermes_cli import kanban_resume_requester as rr
from hermes_cli import kanban_supervisor_requests as sr
from tests.hermes_cli import test_kanban_worker_node as worker_node

gws = worker_node.gws
svc = worker_node.svc


def _foreign_park(home, worker):
    home.enter()
    target = kb.create_task(home.conn, title="recover foreign execution", assignee="default", runs_on=["linux"])
    home.round()
    worker.round()
    worker.tick()
    worker.round()
    run = kb.get_task(worker.conn, target).current_run_id
    assert run is not None
    out = kc.continue_task(worker.conn, target, checkpoint="progress", expected_run_id=run, max_continuations=0)
    assert out["reason_code"] == "continuation_exhausted"
    assert kb.block_task(worker.conn, target, kind="transient", expected_run_id=run,
                         reason="continuation_exhausted: split the work or reset the continuation budget")
    worker.round()
    home.round()
    assert rx.exhaustion_refusal(home.conn, target) is None
    return target


def test_recovery_stays_home_after_foreign_target_eviction(gws, svc):
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    [requester] = rr.ensure_recovery_cards(home.conn)
    home.round()
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, target) is None  # real eviction, not manual row removal
    assert requester not in svc.board(worker_node.BOARD)["leases"], "foreign worker claimed the recovery"
    home.round()
    home.tick()
    run = kb.get_task(home.conn, requester).current_run_id
    assert run is not None
    assert svc.board(worker_node.BOARD)["leases"][requester]["holder"] == home.name
    out = sr.submit(home.conn, env_task=requester, env_run=str(run),
                    ops=[{"op": "resume", "task_id": target}], reason="checkpoint made progress")
    assert sr.drain(home.conn)["processed"] == [(out["request_id"], "done")]
    assert sr.drain(home.conn)["processed"] == []
    assert kb.get_task(home.conn, target).status == "ready"
    assert len(rx._resume_events(home.conn, target)) == 1
    home.round()
    worker.round()
    events = svc.board(worker_node.BOARD)["rows"]["task_events"].values()
    assert sum(e["task_id"] == target and e["kind"] == "unblocked" for e in events) == 1


def test_unpinned_requester_cannot_resume_after_real_foreign_pickup(gws):
    """Control reproduces the old portable-card failure without changing production policy."""
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    requester = kb.create_task(home.conn, title="portable recovery control", assignee="default",
                               workspace_kind="scratch", creator_task_id=target)
    home.round()
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, target) is None
    assert kb.get_task(worker.conn, requester).current_run_id is not None
    assert rx.refusal(worker.conn, requester, target) == f"unknown task {target}"


def test_legacy_null_home_target_still_gets_one_local_recovery(gws):
    from tests.hermes_cli.test_kanban_sync_reconcile import _without_capture

    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    # Legacy local rows can predate ownership stamping. NULL is not proof of a
    # foreign copy; ordinary worker pickup excludes such rows.
    _without_capture(home.conn, "UPDATE tasks SET created_on = NULL WHERE id = ?", (target,))
    [rec] = rr.ensure_recovery_cards(home.conn)
    assert rr.ensure_recovery_cards(home.conn) == []
    assert rx.refusal(home.conn, rec, target) is None
