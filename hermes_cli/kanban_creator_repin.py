"""A scoped worker repins a worker-first card its own task created. [daveey]

The no-pool-worker notice (``kanban_repin_notice``) tells a card's creator to repin it to the origin. When the
creator is a chat, it runs the printed ``hermes kanban move``. When the creator is another card's worker, that CLI
is fenced (worker shells may not write the board) and ``kanban_move`` was scoped to the worker's own card, so the
card waited forever. This is the narrow grant: ``kanban_move(task_id=<card my task created>)`` takes the same
explicit Board repin (``kanban_worker_repin.repin_to_origin`` via ``kanban_move.move_queued``) and nothing else:
not a running card, not a card another task created, not a card without a worker-first policy.
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
        return {"ok": False, "reason_code": "not_creator_task",
                "error": f"worker is scoped to task {env_task}; it may move another card only to repin a "
                         f"worker-first card its own task created, and {task_id} was not created by it"}
    if not repin.applies(conn, task_id):
        return {"ok": False, "reason_code": "not_repinnable",
                "error": f"{task_id} is not an unstarted worker-first card (running, done, archived or no "
                         "placement policy); only the explicit origin repin is allowed here"}
    return km.move_queued(conn, task_id, runs_on=runs_on, reason=reason, hub=hub)
