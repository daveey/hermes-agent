"""A scoped worker repins a worker-first card its own task created. [daveey]

The no-pool-worker notice (``kanban_repin_notice``) tells a card's creator to repin it to the origin. When the
creator is a chat, it runs the printed ``hermes kanban move``. When the creator is another card's worker, that CLI
is fenced (worker shells may not write the board) and ``kanban_move`` was scoped to the worker's own card, so the
card waited forever. This is the narrow grant: ``kanban_move(task_id=<card my task created>)`` takes the same
explicit Board repin (``kanban_worker_repin.repin_to_origin`` via ``kanban_move.move_queued``) and nothing else:
not a running card, not a card another task created, not a card without a worker-first policy.

A second, equally narrow grant covers an orphaned wait (``kanban_repin_owner``): the generated
``repin-owner:<target>`` supervisor may make the same origin repin of exactly its one listed target
(``supervisor_refusal``). It is not a general owner-wake move power: no other card, board or target.

A third grant covers a stranded card (``stranded_refusal``, self-heal agent-7489fcf5cd): any scoped worker may
re-route a READY, never-claimed card of this gateway whose stored ``runs_on`` no enrolled machine carries (rows
written before the create-time check, e.g. ``["hermes-hub"]``) to labels an active machine does carry. The registry
must be known; a satisfiable placement is never touched. Without it the worker could only ask David to edit the card.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable

from hermes_cli.kanban_archive_scope import _creator_task_id


def other_card(args: dict) -> bool:
    """A scoped worker named a task other than its own."""
    env, tid = os.environ.get("HERMES_KANBAN_TASK"), str(args.get("task_id") or "")
    return bool(env and tid and tid != env)


def tool_repin(args: dict, *, board: Callable, check: Callable, ok: Callable, reject_child: Callable) -> str:
    """``kanban_move`` for another card (helpers injected from tools.kanban_tools to keep that facade small)."""
    reject_child("kanban_move", args)
    env, tid = os.environ["HERMES_KANBAN_TASK"], str(args["task_id"])
    reason = str(args.get("reason") or "").strip()
    check(reason, "reason is required")
    with board(args.get("board")) as (_kb, conn):
        res = creator_repin(conn, tid, env, runs_on=args.get("runs_on"), reason=reason,
                            host_reason=args.get("host_reason"))
    check(res.get("ok"), f"could not repin {tid}: {res.get('error')}")
    return ok(**{k: v for k, v in res.items() if k not in ("ok", "task_id", "next")},
              task_id=tid, next="Repinned another card; this run continues.")


def creator_repin(conn: sqlite3.Connection, task_id: str, env_task: str, *, runs_on: Any, reason: str,
                  hub: Any = None, host_reason: str | None = None) -> dict[str, Any]:
    """``move_queued``'s result, or ``{"ok": False, "reason_code", "error"}`` when this worker may not repin it."""
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_repin as repin

    if _creator_task_id(conn, task_id) != env_task:
        refusal = supervisor_refusal(conn, task_id, env_task, runs_on)
        if refusal is not None:
            stranded = stranded_refusal(conn, task_id)
            if stranded is None:
                return stranded_reroute(conn, task_id, env_task, runs_on=runs_on, reason=reason, hub=hub,
                                        host_reason=host_reason)
            from hermes_cli import kanban_pickup_stall_repin as stall
            stalled = stall.refusal(conn, task_id, env_task, runs_on, host_reason=host_reason)
            if stalled is None:
                return stall.move(conn, task_id, env_task, runs_on=runs_on, reason=reason, hub=hub,
                                  host_reason=host_reason)
            return {"ok": False, "reason_code": "not_creator_task",
                    "error": f"worker is scoped to task {env_task}; it may move another card only to repin a "
                             f"worker-first card its own task created (or, as its generated repin-owner "
                             f"supervisor, its one listed orphaned card; or, as a pickup-stall alarm, a card it "
                             f"lists off the stalled machine), or to re-route a ready card whose runs_on no "
                             f"enrolled machine carries, and {task_id} is none of these ({refusal}; {stranded}; "
                             f"pickup-stall alarm: {stalled})"}
    if not repin.applies(conn, task_id):
        # The creator is the likeliest worker to find its own stranded child: the reroute open to every
        # worker is open to it too.
        stranded = stranded_refusal(conn, task_id)
        if stranded is None:
            return stranded_reroute(conn, task_id, env_task, runs_on=runs_on, reason=reason, hub=hub,
                                    host_reason=host_reason)
        return {"ok": False, "reason_code": "not_repinnable",
                "error": f"{task_id} is not an unstarted worker-first card (running, done, archived or no "
                         f"placement policy) nor a ready card stranded on impossible runs_on ({stranded}); "
                         "only the explicit origin repin or a stranded reroute is allowed here"}
    # The origin repin carries its own target state: the worker-first card goes back to the machine it was written
    # for (kanban_move_pool.check needs a host_reason for any machine:<x>, t_01a1d95c41620e6c).
    if _names_machine(runs_on) and not (host_reason or "").strip():
        host_reason = ("origin repin of an unstarted worker-first card: the origin host holds the work it was "
                       "created from")
    return km.move_queued(conn, task_id, runs_on=runs_on, reason=reason, hub=hub, host_reason=host_reason)


def _names_machine(runs_on: Any) -> bool:
    """Whether ``runs_on`` pins one machine (``machine:<x>``), the target that needs a host reason."""
    return isinstance(runs_on, list) and any(isinstance(x, str) and x.startswith("machine:") for x in runs_on)


REROUTE_AUTHOR = "kanban-reroute"


def _registry_view(conn: sqlite3.Connection) -> tuple[str | None, dict | None]:
    """``(me, machines)`` from the Board registry, ``(me, None)`` when it is unknown (unreachable or empty)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    me = (sync.sync_state(conn) or {}).get("gateway_id")
    try:
        reg = wn.registry()
    except Exception:  # health: allow BLE001 -- unknown registry: refuse, never guess impossibility
        reg = None
    machines = (reg or {}).get("machines") or None
    return (str(me) if me else None), machines


def stranded_refusal(conn: sqlite3.Connection, task_id: str) -> str | None:
    """None when ``task_id`` is a ready, never-claimed card of this gateway whose stored ``runs_on`` no enrolled
    machine carries (the create-time check of PR #971 only guards new rows), so any scoped worker may re-route it
    with ``kanban_move(task_id=...)``; else why not. An unknown registry is a refusal: impossibility needs evidence.
    The new labels are checked by ``kanban_move.move_queued`` (an active, unheld machine must carry them)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_runs_on_capabilities as roc
    from hermes_cli import kanban_worker_node as wn
    if sync.board_mode(conn) != "primary":
        return "board is not a Board primary"
    row = conn.execute("SELECT status, runs_on, claim_lock, lease_holder, current_run_id, created_on FROM tasks "
                       "WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return f"no card {task_id} here"
    me, machines = _registry_view(conn)
    if row["created_on"] not in (None, me):
        return f"{task_id} was created on {row['created_on']}; ask its home (operator call)"
    if row["status"] != "ready" or row["claim_lock"] or row["lease_holder"] or row["current_run_id"] is not None:
        return f"{task_id} is not a ready, unclaimed card"
    labels = wn._parse(row["runs_on"])
    if not labels:
        return f"{task_id} has no runs_on labels to re-route"
    if machines is None or not me:
        return "the Board machine registry is unknown, so impossible labels cannot be proven"
    if not roc.impossible(labels, machines, me):
        return f"runs_on={labels} is carried by an enrolled machine (a deliberate placement)"
    return None


def stranded_reroute(conn: sqlite3.Connection, task_id: str, env_task: str, *, runs_on: Any, reason: str,
                     hub: Any = None, host_reason: str | None = None) -> dict[str, Any]:
    """Re-route a card :func:`stranded_refusal` cleared: ``move_queued`` (which refuses unsatisfiable new labels),
    then a comment keeping the old labels so the rewrite can be undone, and a note on any open ask that names it.
    A ``machine:<x>`` target carries the stranded labels as its host reason (``kanban_move_pool.check``): the old
    labels named the host the card was written for, which no enrolled machine carries under that name."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_node as wn
    old = wn._parse(conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])
    if _names_machine(runs_on) and not (host_reason or "").strip():
        host_reason = (f"stranded reroute: stored runs_on {old} names a host no enrolled machine carries; the new "
                       "labels name the machine it meant")
    res = km.move_queued(conn, task_id, runs_on=runs_on, reason=reason, hub=hub, host_reason=host_reason,
                         event_extra={"rerouted_by": env_task, "stranded_runs_on": old})
    if not res.get("ok"):
        return res
    note = (f"Re-routed by {env_task}: runs_on {old} matched no enrolled machine (stranded in ready); now "
            f"{res.get('runs_on')}. Reason: {reason[:500]}. Previous runs_on kept here for undo (no machine "
            "carries it, so restoring it strands the card again).")
    asks = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status = 'blocked' AND id != ? AND human_request LIKE ?",
        (task_id, f"%{task_id}%")).fetchall()]
    with kb.write_txn(conn):
        kb.add_comment(conn, task_id, REROUTE_AUTHOR, note)
        for ask in asks:
            kb.add_comment(conn, ask, REROUTE_AUTHOR,
                           f"Superseded: the board step this ask names on {task_id} was done by an agent "
                           f"({env_task} re-routed it to {res.get('runs_on')}). Archive this ask unless it asks "
                           "for something else.")
    return {**res, "rerouted_from": old, "superseded_asks": asks}


def supervisor_refusal(conn: sqlite3.Connection, task_id: str, env_task: str, runs_on: Any) -> str | None:
    """None when ``env_task`` is the generated repin-owner supervisor of ``task_id`` and the move is its one
    allowed action (the origin repin of a still-orphaned, unstarted, no-pool-worker wait); else why not.

    Every condition is checked on ``conn``, the board the move would write: the supervisor must be an open card
    there whose generated identity (creator, idempotency key, exact title, listed line) names exactly this board
    and target. ``move_queued``/``repin_to_origin`` then re-check the Board-side preconditions.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_repin_notice as notice
    from hermes_cli import kanban_repin_owner as owner
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_requeue import owner_wake_listed_refs
    from hermes_cli.kanban_worker_placement import POLICY

    worker = kb.get_task(conn, env_task)
    board = owner.board_slug(conn)
    if (worker is None or worker.created_by != "owner-wake" or worker.status in owner.CLOSED
            or worker.idempotency_key != owner.idempotency_key(task_id)
            or worker.title != owner.title(board, task_id)
            or owner_wake_listed_refs(worker) != [(board, task_id)]):
        return "not its generated repin-owner supervisor on this board"
    if sync.board_mode(conn) != "primary":
        return "board is not a Board primary"
    gateway = (sync.sync_state(conn) or {}).get("gateway_id")
    try:
        labels = list(wn.normalize_runs_on(runs_on) or [])
    except ValueError:
        labels = None
    if not gateway or labels != [f"machine:{gateway}"]:
        return f"the only allowed target is runs_on=['machine:{gateway}']"
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (row is None or row["placement_policy"] != POLICY or row["created_on"] != gateway or row["status"] != "ready"
            or row["claim_lock"] or row["lease_holder"] or row["started_at"] is not None
            or row["current_run_id"] is not None
            or conn.execute("SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (task_id,)).fetchone()):
        return "target is not an unstarted ready worker-first card created on this origin"
    if not notice.awaiting(conn, task_id):
        return "target is not in a current no-pool-worker wait"
    return None
