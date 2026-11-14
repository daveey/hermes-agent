"""Moving an orderly PR owner preserves its dependency checkpoint across gateways."""
import time
from argparse import Namespace

from hermes_cli import kanban as cli
from hermes_cli import kanban_db_connect as kbc

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_move as km
from tests.hermes_cli import test_kanban_dependency_pr_handoff as helpers
from tests.hermes_cli import test_kanban_move as moves

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

svc = moves.svc
gws = moves.gws


@pytest.mark.parametrize("reclaim", [False, True])
def test_running_pr_owner_move(gws, monkeypatch, reclaim):
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    hub, worker = gws
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    task, run = moves._running_hub_card(hub)
    worker_comment(hub.conn, task, "default", helpers.PR)
    stopped = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda pid, lock, **kw: stopped.append(pid) or {
        "prev_pid": pid, "host_local": True, "termination_attempted": True,
        "terminated": True, "sigkill": False})
    if reclaim:
        result = km.move_reclaim(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="operator reclaim")
        assert stopped
    else:
        result = km.move_task(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="capacity",
                              checkpoint="Resume the existing PR", expected_run_id=run)
    assert result["ok"], result
    hub.round()
    worker.round()
    tick = worker.tick()
    assert ((task, "active_pr") in tick.respawn_guarded) is reclaim
    assert kb.get_task(worker.conn, task).status == ("ready" if reclaim else "running")


@pytest.mark.parametrize("publication", [helpers.PR, "https://github.com/example/repo/pull/11"])
def test_publication_after_park_is_not_authorized_by_move(gws, monkeypatch, publication):
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    hub, worker = gws
    hub.enter()
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    task, _, clock = helpers.park(hub.conn, monkeypatch)
    clock[0] += 1
    worker_comment(hub.conn, task, "dev", publication)
    assert km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="capacity",
                          checkpoint="Resume PR")["ok"]
    hub.round()
    worker.round()
    assert (task, "active_pr") in worker.tick().respawn_guarded


def test_parent_result_refresh_wakes_without_status_change(gws, monkeypatch):
    import json
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_move_parent_evidence import result

    hub, worker = gws
    hub.enter()
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    task, _, clock = helpers.park(hub.conn, monkeypatch)
    parent = kb.parent_ids(hub.conn, task)[0]
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_runs SET metadata = ? WHERE task_id = ?",
                         (json.dumps({"verdict": "approve", "reviewed_sha": "short", "pr": helpers.PR}), parent))
    clock[0] += 1
    assert km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="capacity",
                          checkpoint="Resume PR")["ok"]
    hub.round()
    worker.round()
    assert (task, "active_pr") in worker.tick().respawn_guarded
    assert result(worker.conn, task, parent)["parent_status"] == "done"
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_runs SET metadata = ? WHERE task_id = ?",
                         (json.dumps({"verdict": "approve", "reviewed_sha": "a" * 40, "pr": helpers.PR}), parent))
    assert sync.refresh_remote_parents(worker.conn, hub.conn) == [parent]
    assert prs.active_pr_reason(worker.conn, task, int(time.time())) is None
    before = worker.conn.total_changes
    assert sync.refresh_remote_parents(worker.conn, hub.conn) == []
    assert worker.conn.total_changes == before
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    assert sync.refresh_remote_parents(worker.conn, hub.conn) == [parent]
    assert result(worker.conn, task, parent)["parent_status"] == "ready"
    assert prs.active_pr_reason(worker.conn, task, int(time.time())) == "active_pr"


def test_operator_cli_preserves_checkpoint(gws, monkeypatch):
    hub, _ = gws
    hub.enter()
    task = kb.create_task(hub.conn, title="move queued card", assignee="dev")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    args = Namespace(task_id=task, runs_on="machine:worker-1", reason="capacity", host_reason=HR,
                     checkpoint="Resume the existing PR, not a new implementation.",
                     force=False, reclaim=False, json=True)
    assert cli._cmd_move(args) == 0
    with kbc.connect_closing() as conn:
        event = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved'",
                             (task,)).fetchone()
        assert kb._json_dict(event[0]).get("checkpoint") == args.checkpoint



@pytest.mark.parametrize("review,checkpoint,allowed,owned", [
    (False, "Resume existing PR", True, True),
    (True, "Resume existing PR", True, True),
    # No move checkpoint: the owner's authenticated review park alone scopes the
    # foreign-parent mirror on the importing worker (t_01a1cda0a1585230). [daveey]
    (False, "", True, True),
    (True, "", True, True),
    ("invalid", "Resume existing PR", False, True),
    ("invalid", "", False, True),
    # Neither a move checkpoint nor an authenticated park: nothing to read, stays held.
    (False, "", False, False),
])
def test_operator_move_after_dependency_park(gws, monkeypatch, review, checkpoint, allowed, owned):
    hub, worker = gws
    hub.enter()
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    task, _, clock = helpers.park(hub.conn, monkeypatch, finish=False, owned=owned)
    clock[0] += 1
    parent = kb.parent_ids(hub.conn, task)[0]
    assert kb.claim_task(hub.conn, parent)
    metadata = {"verdict": "approve", "reviewed_sha": "a" * 40, "pr": helpers.PR} if review else {}
    if review == "invalid":
        metadata["reviewed_sha"] = "short"
    assert kb.complete_task(hub.conn, parent, summary="prerequisite finished", metadata=metadata)
    kb.recompute_ready(hub.conn)
    clock[0] += 1
    result = km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="continue on worker",
                            checkpoint=checkpoint)
    assert result["ok"], result
    hub.round()
    worker.round()
    tick = worker.tick()
    if not allowed:
        assert (task, "active_pr") in tick.respawn_guarded
        return
    assert (task, "active_pr") not in tick.respawn_guarded
    moved = kb.get_task(worker.conn, task)
    assert moved is not None and moved.status == "running", vars(tick)
    assert prs.active_pr_reason(worker.conn, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("outcome", ["crashed", "timed_out", "reclaimed"])
def test_move_after_failed_run_stays_guarded(gws, monkeypatch, outcome):
    hub, worker = gws
    hub.enter()
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    task, run, clock = helpers.park(hub.conn, monkeypatch)
    clock[0] += 1
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_runs SET outcome = ? WHERE id = ?", (outcome, run))
        kb._append_event(hub.conn, task, outcome, {}, run_id=run)
    result = km.move_queued(hub.conn, task, runs_on=["machine:worker-1"], host_reason=HR, reason="continue on worker",
                            checkpoint="Resume existing PR after dependency completion")
    assert result["ok"], result
    assert prs.active_pr_reason(hub.conn, task, int(time.time())) == "active_pr"
    hub.round()
    worker.round()
    tick = worker.tick()
    assert (task, "active_pr") in tick.respawn_guarded
