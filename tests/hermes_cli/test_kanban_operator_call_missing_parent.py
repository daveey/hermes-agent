"""Operator-call unblock of a parent_missing child by the repair card its block names. [daveey]

The dispatcher's parent_missing block carries no agent actor, so the home refused every operator-call unblock of it
as "a hand, Desktop or policy hold", although the same block names the repair card as its owner. The repair worker
then ran ``hermes kanban unblock`` over ssh and the event read ``actor: user``. The home now accepts exactly the
repair card its newest parent_missing block names (Board-proven ``caller.task_id``), and nothing else.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_supervisor_requests as ksr
from tests.hermes_cli import test_kanban_operator_call as base

_log, _round = base._log, base._round
fleet = base.fleet  # the two-gateway fixture, shared


def _home_repair(f):
    """The home blocks a child as parent_missing and files its repair card; a worker-5 run picks the repair up."""
    child = f.card(f.h, "dependent of a lost parent")
    repair = kb.create_task(f.h, title=f"Recover missing dependencies for {child}", assignee="default",
                            created_by="missing-parent-detector", creator_task_id=child)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET created_on = 'hub' WHERE id = ?", (repair,))
        f.h.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (child,))
        kb._append_event(f.h, child, "blocked", {
            "reason": f"parent t_ghost missing from this board; repair card {repair} owns investigation and recovery",
            "kind": "transient", "source": "parent_missing", "parents": ["t_ghost"], "repair_task_id": repair})
    # Worker-node pickup: worker-5 holds a copy of the hub's repair card and runs it under a Board lease.
    row = f.h.execute("SELECT * FROM tasks WHERE id = ?", (repair,)).fetchone()
    with kb.write_txn(f.w):
        f.w.execute(f"INSERT INTO tasks ({','.join(row.keys())}) VALUES ({','.join('?' * len(row))})", tuple(row))
    kb.claim_task(f.w, repair)
    run_id = kb._current_run_id(f.w, repair)
    f.run_uid(f.w, repair)
    f.board()
    run = f.w.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    cols = [k for k in run.keys() if k != "id"]
    with kb.write_txn(f.rep):
        f.rep.execute(f"INSERT INTO task_runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      tuple(run[k] for k in cols))
    return child, repair, run_id


def _submit_as(f, task, run_id, target):
    return ksr.submit(f.w, env_task=task, env_run=str(run_id),
                      ops=[{"op": "operator", "action": "unblock", "task_id": target, "reason": "parent restored"}])


def _execute_row(f, target):
    return next(r for r in _log(f.h, "execute") if r["target_task_id"] == target)


def _unblocked(f, target):
    return [json.loads(p or "null") for (p,) in f.h.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'unblocked' ORDER BY id", (target,))]


def test_repair_card_unblocks_its_child_through_an_operator_call(fleet):
    f = fleet
    child, repair, run_id = _home_repair(f)
    out = _submit_as(f, repair, run_id, child)
    _round(f)
    assert _execute_row(f, child)["outcome"] == "applied", _execute_row(f, child)
    assert kb.get_task(f.h, child).status in ("ready", "todo")
    [payload] = _unblocked(f, child)
    assert payload["operator_request_uid"]
    assert payload["operator_caller"]["task_id"] == repair
    assert payload["operator_caller"]["gateway"] == "worker-5"
    assert payload["repair_task_id"] == repair
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"


def test_another_caller_still_cannot_release_the_parent_missing_block(fleet):
    f = fleet
    child, _repair, _ = _home_repair(f)
    _submit_as(f, f.caller, f.run_id, child)
    _round(f)
    row = _execute_row(f, child)
    assert row["outcome"] == "refused" and "not blocked by its own agent run" in row["detail"]
    assert kb.get_task(f.h, child).status == "blocked"
    assert not _unblocked(f, child)


def test_repair_card_cannot_unblock_another_hub_card(fleet):
    f = fleet
    _child, repair, run_id = _home_repair(f)
    held = f.card(f.h, "held by hand")
    kb.block_task(f.h, held, reason="hold", actor="user", kind="transient")
    f.board()
    _submit_as(f, repair, run_id, held)
    _round(f)
    assert _execute_row(f, held)["outcome"] == "refused"
    assert kb.get_task(f.h, held).status == "blocked"


def test_block_superseded_by_a_hand_block_is_refused_at_home(fleet):
    f = fleet
    child, repair, run_id = _home_repair(f)
    with kb.write_txn(f.h):
        kb._append_event(f.h, child, "blocked", {"reason": "David holds it", "actor": "user", "kind": "transient"})
    f.board()
    _submit_as(f, repair, run_id, child)
    _round(f)
    assert _execute_row(f, child)["outcome"] == "refused"
    assert kb.get_task(f.h, child).status == "blocked"


def test_parent_missing_block_naming_another_repair_card_is_refused(fleet):
    f = fleet
    child, repair, run_id = _home_repair(f)
    with kb.write_txn(f.h):
        kb._append_event(f.h, child, "blocked", {"reason": "re-detected", "kind": "transient",
                                                 "source": "parent_missing", "repair_task_id": "t_0123456789abcdef"})
    f.board()
    _submit_as(f, repair, run_id, child)
    _round(f)
    assert _execute_row(f, child)["outcome"] == "refused"
    assert kb.get_task(f.h, child).status == "blocked"
