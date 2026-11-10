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
from contextlib import contextmanager
from typing import Optional


@contextmanager
def granted_board(board: Optional[str], tid: str):
    """Resolve an exact owner-wake grant, never changing the global worker pin.

    Authority is read on the worker's pinned board; only the listed target is
    opened on its own local board. Ordinary workers and descendants keep the
    existing resolution. Omitted board is inferred only for a unique listing.
    """
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_requeue import owner_wake_listed_refs

    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and kt._is_dispatcher_owned_worker():
        with kt._board(None) as (kb, own):
            worker = kb.get_task(own, env_tid)
            refs = [(slug, target) for slug, target in
                    owner_wake_listed_refs(worker)
                    if target == tid and (board is None or board == slug)]
            kt._check(len(refs) < 2, "ambiguous owner-wake target; specify board")
            if len(refs) == 1:
                kt._check(worker.status == "running" and
                          kt._worker_run_id(env_tid) is not None and
                          worker.current_run_id == kt._worker_run_id(env_tid),
                          "owner-wake supervisor no longer owns its current run")
                if refs[0][0] == kb.get_current_board():
                    yield kb, own, (env_tid, refs[0][0])
                    return
                path = kb.board_own_db_path(refs[0][0])
                kt._check(path.is_file(), "orchestrator-only: listed owner-wake board is not available locally")
                with kbc.connect_closing(db_path=path) as conn:
                    yield kb, conn, (env_tid, refs[0][0])
                return
    with kt._board(board) as (kb, conn):
        yield kb, conn, None


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
            from hermes_cli.kanban_requeue import has_requeue_grant
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
                "supervisor_board": kb.get_current_board(),
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
        with granted_board(args.get("board"), tid) as (kb, conn, grant):
            from hermes_cli.kanban_requeue import OWNER_WAKE_REQUEUE_STATUSES, requeue_grant
            owner, target_board = grant if grant else (None, None)
            via, source = (("owner-wake-requeue", owner) if owner else
                           requeue_grant(conn, env_tid, tid,
                                         board=args.get("board") or kb.get_current_board()))
            if via:
                def precondition(current):
                    task = kb.get_task(current, tid)
                    if via == "owner-wake-requeue" and (
                            task is None or task.status not in (*OWNER_WAKE_REQUEUE_STATUSES, "ready")):
                        return "owner-wake target must be triage or blocked"
                    if owner:
                        from hermes_cli.kanban_requeue import owner_wake_listed_refs
                        with kt._board(None) as (_, own):
                            worker = kb.get_task(own, owner)
                            refs = [(slug, target) for slug, target in owner_wake_listed_refs(worker)
                                    if target == tid and (args.get("board") is None or args["board"] == slug)]
                            if (worker is None or worker.status != "running" or
                                    worker.current_run_id != kt._worker_run_id(owner) or refs != [(target_board, tid)]):
                                return "owner-wake grant or run was revoked"
                    return None

                task = kb.get_task(conn, tid)
                if owner and task is not None and task.status == "ready":
                    from hermes_cli.kanban_hold_release import release
                    release(conn, tid, supervisor=owner, supervisor_board=kb.get_current_board(),
                            precondition=precondition)
                    return kt._ok(task_id=tid, via="owner-wake-hold-release", status="ready")
                ok = kb.unblock_task(conn, tid, precondition=precondition)
                kt._check(ok, f"could not unblock {tid} (not blocked/triage/scheduled, or unknown)")
                _log_requeue(conn, kb, tid, env_tid, via, source)
                return kt._ok(task_id=tid, via=via, **kt._fields(kb.get_task(conn, tid), ("status",)))
    kt._require_orchestrator_tool("kanban_unblock")
    kt._enforce_worker_task_ownership(tid)
    with kt._board(args.get("board")) as (kb, conn):
        # [daveey] agent-70dc07d1a5: a chat's unblock of a human_request card is David's go. It used to keep its
        # p0 rank and return only the status, so the chat promised a pickup 47 older cards stood in front of.
        from hermes_cli import kanban_chat_wait as cw
        asked: dict = {}

        def note_request(current) -> None:
            asked["human"] = cw.had_human_request(current, tid)  # unblock_task clears human_request

        kt._check(kb.unblock_task(conn, tid, precondition=note_request),
                  f"could not unblock {tid} (not blocked/triage/scheduled, or unknown)")
        try:
            raised = cw.raise_for_human_go(conn, tid) if asked.get("human") else None
        except Exception:  # health: allow BLE001 -- the unblock already committed; the raise is advisory
            kt.logger.warning("kanban_unblock: human_go priority raise failed for %s", tid, exc_info=True)
            raised = None
        subscribed = kt._maybe_subscribe_on_touch(conn, tid)
        task = kb.get_task(conn, tid)
        return kt._ok(task_id=tid, **kt._fields(task, ("status",)),
                      **({"priority_raised": raised} if raised else {}),
                      **cw.unblock_fields(conn, task, args.get("board")),
                      **({"subscribed": True} if subscribed else {}))
