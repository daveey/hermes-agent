"""Requeue grants: the unblocks a worker may perform on another card.

Lives outside ``kanban_tools`` so that file, already over its line cap, does not
grow. The shell fence is unchanged; only the in-process tool has these grants:

- a self-heal fix card (or a card a finished fix card created) may unblock the
  one card named in its defect;
- an owner-wake supervisor card may unblock the cards its body lists while they
  are in triage or blocked (``hermes_cli.kanban.requeue_grant``). [daveey]
"""
from __future__ import annotations

import os
from typing import Optional


def check_unblock() -> bool:
    """Orchestrators always. A worker only when it holds a requeue grant."""
    from tools.kanban_tools import _board, _is_dispatcher_owned_worker, _visible, logger

    if _visible(to_env_worker=False):
        return True
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid or not _is_dispatcher_owned_worker():
        return False
    try:
        with _board(None) as (kb, conn):
            from hermes_cli.kanban import has_requeue_grant
            return has_requeue_grant(conn, env_tid, board=kb.get_current_board())
    except Exception:  # health: allow BLE001 -- tool-list gate fails closed; traceback is logged
        logger.exception("kanban_unblock grant check failed")
        return False


def _log_requeue(conn, kb, tid: str, env_tid: str, via: str, source: Optional[str]) -> None:
    """Audit row in its own txn. ``unblock_task`` already committed the transition."""
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        if via == "owner-wake-requeue":
            kb._append_event(conn, tid, "owner_wake_requeue", {
                "by": env_tid, "supervisor_card": source, "target": tid,
            })
        else:
            kb._append_event(conn, tid, "self_heal_requeue", {
                "by": env_tid, "fix_card": source, "target": tid,
            })


def handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked/triage/scheduled task, or a card this worker holds a requeue grant for."""
    from tools import kanban_tools as kt

    kt._reject_delegated_child_mutation("kanban_unblock")
    tid = args.get("task_id")
    kt._check(tid, "task_id is required")
    tid = str(tid)
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and tid != env_tid:
        with kt._board(args.get("board")) as (kb, conn):
            from hermes_cli.kanban import requeue_grant
            via, source = requeue_grant(conn, env_tid, tid, board=args.get("board") or kb.get_current_board())
            if via:
                ok = kb.unblock_task(conn, tid)
                kt._check(ok, f"could not unblock {tid} (not blocked/triage/scheduled, or unknown)")
                _log_requeue(conn, kb, tid, env_tid, via, source)
                return kt._ok(task_id=tid, via=via, **kt._fields(kb.get_task(conn, tid), ("status",)))
    kt._require_orchestrator_tool("kanban_unblock")
    kt._enforce_worker_task_ownership(tid)
    with kt._board(args.get("board")) as (kb, conn):
        kt._check(kb.unblock_task(conn, tid), f"could not unblock {tid} (not blocked/triage/scheduled, or unknown)")
        subscribed = kt._maybe_subscribe_on_touch(conn, tid)
        return kt._ok(task_id=tid, **kt._fields(kb.get_task(conn, tid), ("status",)),
                      **({"subscribed": True} if subscribed else {}))
