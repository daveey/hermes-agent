"""Operator-call unblock of a parent_missing child by the repair card its block names. [daveey]

The dispatcher's parent_missing block carries no agent actor, so the home refused every operator-call unblock of it
as "a hand, Desktop or policy hold", although the same block names the repair card as its owner. The repair worker
then ran ``hermes kanban unblock`` over ssh and the event read ``actor: user``. The home now accepts exactly the
repair card its newest parent_missing block names (Board-proven ``caller.task_id``), and nothing else.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as kox
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


def test_breaker_hold_after_a_release_is_refused_at_home(fleet):
    """Review 5888194553: released after the parent_missing block, then re-held by the breaker (``gave_up``, no
    ``blocked`` event). The newest ``blocked`` event still names the repair card, but the hold is a policy hold."""
    f = fleet
    child, repair, run_id = _home_repair(f)
    assert kb.unblock_task(f.h, child)
    with kb.write_txn(f.h):
        kb._append_event(f.h, child, "gave_up", {"kind": "transient", "reason_code": "worker_failures_exhausted",
                                                 "sticky": True})
        f.h.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (child,))
    f.board()
    _submit_as(f, repair, run_id, child)
    _round(f)
    row = _execute_row(f, child)
    assert row["outcome"] == "refused" and "not blocked by its own agent run" in row["detail"], row
    assert kb.get_task(f.h, child).status == "blocked"
    assert len(_unblocked(f, child)) == 1  # only the release before the breaker trip


def test_child_that_later_got_a_human_request_is_refused_at_home(fleet):
    """Review 5887767012 nit 3: a human_request on the parent_missing child is David's to answer."""
    f = fleet
    child, repair, run_id = _home_repair(f)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET human_request = ? WHERE id = ?",
                    (json.dumps({"action": "Pick A or B. Unblock = build it; archive = drop it."}), child))
    # The caller's replica has not seen the ask yet, so the call reaches the home, which refuses it.
    _submit_as(f, repair, run_id, child)
    _round(f)
    row = _execute_row(f, child)
    assert row["outcome"] == "refused" and "waits on a human" in row["detail"], row
    assert kb.get_task(f.h, child).status == "blocked"
    assert not _unblocked(f, child)
    # Once the replica has it, the caller refuses before sending.
    f.board()
    with pytest.raises(ksr.SupervisorRequestError, match="waits on a human"):
        _submit_as(f, repair, run_id, child)


def test_breaker_hold_after_releasing_an_own_agent_block_is_refused_at_home(fleet):
    """The pre-existing own-actor check had the same blind spot: the card's own run blocked it, it was released,
    then the breaker re-held it (``gave_up``, no ``blocked`` event). That hold is not the agent's own block."""
    f = fleet
    held = f.card(f.h, "own block, released, then breaker")
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (held,))
        kb._append_event(f.h, held, "blocked", {"reason": "waiting", "kind": "transient",
                                                "actor": f"agent (task {held})"})
    assert kb.unblock_task(f.h, held)
    with kb.write_txn(f.h):
        kb._append_event(f.h, held, "gave_up", {"kind": "transient", "reason_code": "worker_failures_exhausted",
                                                "sticky": True})
        f.h.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (held,))
    f.board()
    _submit_as(f, f.caller, f.run_id, held)
    _round(f)
    row = _execute_row(f, held)
    assert row["outcome"] == "refused" and "not blocked by its own agent run" in row["detail"], row
    assert kb.get_task(f.h, held).status == "blocked"


def test_worker_kanban_unblock_tool_forwards_to_the_home_end_to_end(fleet, monkeypatch):
    """Review 5889810989 nit 4: the repair worker's own ``kanban_unblock`` call on worker-5, with no stub between
    the tool and the home. The tool sees the child only in the Board replica (``kfc.refusal``), files the operator
    call (``handle_supervisor_request`` -> ``ksr.submit``), and the hub applies it attributed to the repair card."""
    from pathlib import Path

    from hermes_cli import kanban_foreign_card as kfc
    from hermes_cli.kanban_db_connect import _main_db_file
    from tools import kanban_tools as kt

    f = fleet
    child, repair, run_id = _home_repair(f)
    # A Board-synced copy carries the repair card's created event (its creator_task_id is the child).
    created = f.h.execute("SELECT kind, payload, created_at, uid FROM task_events WHERE task_id = ? AND "
                          "kind = 'created'", (repair,)).fetchone()
    with kb.write_txn(f.w):
        f.w.execute("INSERT INTO task_events (task_id, kind, payload, created_at, uid) VALUES (?,?,?,?,?)",
                    (repair, *created))
    worker_db, replica_db = _main_db_file(f.w), _main_db_file(f.rep)
    monkeypatch.setenv("HERMES_KANBAN_DB", worker_db)
    # The tool opens its own connection to worker-5's file; the fixture names gateways per connection object.
    monkeypatch.setattr(kox, "_own_gateway",
                        lambda c: f.name(c) or ("worker-5" if _main_db_file(c) == worker_db else None))
    monkeypatch.setattr(kfc, "replica_path",
                        lambda p: Path(replica_db) if p and Path(p).resolve() == Path(worker_db).resolve() else None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", repair)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert kb.get_task(f.w, child) is None and kfc.refusal(f.w, child)

    out = json.loads(kt._handle_unblock({"task_id": child}))
    assert out.get("ok") is True and out["status"] == "pending", out
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):  # the drains run in the gateways, not the worker
        monkeypatch.delenv(var)
    _round(f)
    assert _execute_row(f, child)["outcome"] == "applied", _execute_row(f, child)
    assert kb.get_task(f.h, child).status in ("ready", "todo")
    [payload] = _unblocked(f, child)
    assert payload["operator_caller"] == {**payload["operator_caller"], "task_id": repair, "gateway": "worker-5"}
    assert payload["repair_task_id"] == repair
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
