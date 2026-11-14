"""A review parked on one gateway releases the respawn guard on the card's home gateway. [daveey]

Production shape (t_01a1cde626be2ada, t_01a1c59e0410b2fb): a hub card is moved to a worker with a
plain queued move (no checkpoint), the worker's run publishes a PR, creates an exact-head review card
on the worker and parks on it. The review is worker-owned, so on the hub it exists only in the Board
replica, never in the hub's live ``tasks``/``task_runs``. The review completes; the hub must let the
owner resume instead of holding ``active_pr`` forever. Integer run/comment ids differ between the two
files throughout, so only portable identity may connect them.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_move as km
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli import test_kanban_move as moves

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

PR = "https://github.com/example/repo/pull/10"
HEAD = "a" * 40
gws = moves.gws
svc = moves.svc


def _claimed_run(conn, task):
    return conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task,)).fetchone()[0]


def _park_on_worker_review(hub, worker, monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    hub.enter()
    task = kb.create_task(hub.conn, title="implement PR", assignee="default")
    # Like production: a plain capacity move, no checkpoint text.
    assert km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="capacity")["ok"]
    hub.round()
    worker.round()
    worker.tick()
    worker.enter()
    run = _claimed_run(worker.conn, task)
    assert run is not None, "worker must claim the moved card"
    worker_comment(worker.conn, task, "default", f"Implementation published as {PR} at head {HEAD}")
    review = kb.create_task(worker.conn, title="exact-head review", assignee="reviewer")
    kb.link_tasks(worker.conn, review, task, expected_child_run_id=run)
    assert kb.block_task(worker.conn, task, kind="dependency", reason="await review", expected_run_id=run)
    worker.round()
    hub.round()
    return task, review


def _finish_review(hub, worker, review, metadata):
    worker.enter()
    kb.claim_task(worker.conn, review)
    assert kb.complete_task(worker.conn, review, summary="review done", metadata=metadata)
    worker.round()
    hub.round()
    worker.round()
    hub.round()
    hub.enter()
    kb.recompute_ready(hub.conn)


def _guard(gw, task):
    gw.enter()
    return kbd.check_respawn_guard(gw.conn, task)


@pytest.mark.parametrize("verdict", ["request_changes", "approve"])
def test_foreign_review_releases_guard_on_home_gateway(gws, monkeypatch, verdict):
    hub, worker = gws
    task, review = _park_on_worker_review(hub, worker, monkeypatch)
    assert kb.get_task(hub.conn, review) is None, "the review must be foreign to the hub's live file"
    hub_run = hub.conn.execute("SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                               (task,)).fetchone()[0]
    worker_run = worker.conn.execute("SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                                     (task,)).fetchone()[0]
    park = json.loads(hub.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (task,)).fetchone()[0])
    assert park["review_checkpoint"]["run_id"] == worker_run
    del hub_run  # ids may or may not collide in a fixture; identity must never rely on them
    _finish_review(hub, worker, review, {"verdict": verdict, "reviewed_sha": HEAD, "pr": PR})
    assert kb.get_task(hub.conn, task).status == "ready"
    assert _guard(hub, task) is None


def test_foreign_review_without_exact_head_still_holds(gws, monkeypatch):
    hub, worker = gws
    task, review = _park_on_worker_review(hub, worker, monkeypatch)
    _finish_review(hub, worker, review, {"verdict": "approve", "reviewed_sha": "short", "pr": PR})
    assert kb.get_task(hub.conn, task).status == "ready"
    assert _guard(hub, task) == "active_pr"


def test_crash_after_park_with_foreign_review_still_holds(gws, monkeypatch):
    hub, worker = gws
    task, review = _park_on_worker_review(hub, worker, monkeypatch)
    _finish_review(hub, worker, review, {"verdict": "request_changes", "reviewed_sha": HEAD, "pr": PR})
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, task, "crashed", {})
    assert _guard(hub, task) == "active_pr"
