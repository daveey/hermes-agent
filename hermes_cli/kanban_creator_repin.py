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
        res = creator_repin(conn, tid, env, runs_on=args.get("runs_on"), reason=reason)
    check(res.get("ok"), f"could not repin {tid}: {res.get('error')}")
    return ok(**{k: v for k, v in res.items() if k not in ("ok", "task_id", "next")},
              task_id=tid, next="Repinned another card; this run continues.")


def creator_repin(conn: sqlite3.Connection, task_id: str, env_task: str, *, runs_on: Any, reason: str,
                  hub: Any = None) -> dict[str, Any]:
    """``move_queued``'s result, or ``{"ok": False, "reason_code", "error"}`` when this worker may not repin it."""
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_repin as repin

    if _creator_task_id(conn, task_id) != env_task:
        refusal = supervisor_refusal(conn, task_id, env_task, runs_on)
        if refusal is not None:
            return {"ok": False, "reason_code": "not_creator_task",
                    "error": f"worker is scoped to task {env_task}; it may move another card only to repin a "
                             f"worker-first card its own task created (or, as its generated repin-owner "
                             f"supervisor, its one listed orphaned card), and {task_id} is neither ({refusal})"}
    if not repin.applies(conn, task_id):
        return {"ok": False, "reason_code": "not_repinnable",
                "error": f"{task_id} is not an unstarted worker-first card (running, done, archived or no "
                         "placement policy); only the explicit origin repin is allowed here"}
    return km.move_queued(conn, task_id, runs_on=runs_on, reason=reason, hub=hub)


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
