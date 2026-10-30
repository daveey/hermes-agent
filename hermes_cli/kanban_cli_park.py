"""CLI helpers for per-task park/unblock ops, split out of ``hermes_cli/kanban.py``. [daveey]

Reason comments (``_commented``), id validation before any mutation (``_ids_error``), and the
wake-check / awaiting guards a park runs before it lands (``_guarded_park``).
"""
from __future__ import annotations

import argparse
import re
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_output import _err


def _commented(reason: Optional[str], author, prefix: str, op):
    """Wrap a per-task ``op(tid, comment)`` so a ``reason`` is recorded as a ``PREFIX: reason``
    comment by the op itself, in its write txn and only when it lands (``kanban_reason_comment``):
    a refused op leaves no comment claiming a change that never happened. [daveey]"""
    comment = (author, f"{prefix}: {reason}") if reason else None
    return lambda tid: op(tid, comment)


# A task id as the boards mint them (``t_`` + hex; see ``kanban_db._new_task_id``/``kanban_db_sync.new_task_id``).
_TASK_ID_SHAPE = re.compile(r"t_[0-9a-zA-Z]{4,64}")


def _ids_error(ids, hint: str, conn=None) -> int:
    """rc 2 (stderr names the token) when any id is not task-id shaped or, given ``conn``, not on the
    board; 0 when all are. Run before any mutation so free text passed where ids go changes nothing."""
    for tid in ids:
        if not isinstance(tid, str) or not _TASK_ID_SHAPE.fullmatch(tid):
            return _err(f"not a task id: {str(tid)[:40]!r}. {hint}", 2)
    if conn is not None:
        for tid in ids:
            if kb.get_task(conn, tid) is None:
                return _err(f"unknown task id: {tid!r}; nothing was changed", 2)
    return 0


_REASON_FLAG_HINT = 'Pass a reason with --reason "..."'
_REASON_WORDS_HINT = "The reason goes after the task id; --ids takes only task ids"


def _cli_wake_check(args: argparse.Namespace):
    """``(wake_check, check_every, clock_only)`` from ``--wake-check/--check-every/--clock-only``."""
    from hermes_cli import kanban_wake_check as kwc

    cmd = kwc.normalize_cmd(getattr(args, "wake_check", None))
    every = kwc.normalize_every(getattr(args, "check_every", None)) if cmd else None
    err = kwc.dry_run_error(cmd, blocker_id=getattr(args, "blocker_id", None) or None)
    if err:
        raise ValueError(err)
    return cmd, every, bool(getattr(args, "clock_only", False))


def _cli_awaiting(args: argparse.Namespace, wake):
    """``(awaiting, wake)`` from ``--awaiting KIND:REF|JSON`` (+ ``--awaiting-why``); a card wait
    with no ``--wake-check`` gets ``hermes kanban wait-check task`` as its probe. [daveey]"""
    from hermes_cli import kanban_awaiting as kaw

    raw = getattr(args, "awaiting", None)
    if not raw:
        return None, wake
    awaiting = kaw.normalize(raw)
    why = getattr(args, "awaiting_why", None)
    if why and awaiting is not None:
        awaiting["why"] = why.strip()[:600]
    probe = None if wake[0] else kaw.card_probe(awaiting)
    if probe:
        from hermes_cli import kanban_wake_check as kwc
        wake = (probe, kwc.normalize_every(None), wake[2])
    kaw.confirm_remote(awaiting)  # ssh, so before the park's write txn
    return awaiting, wake


def _guarded_park(conn, op: str, wake, refused: dict, park, awaiting=None, **plan):
    """Per-task op: refuse a park > 2 h out with no probe (``kanban_wake_check.guard_error``)
    or one nothing would produce (``kanban_awaiting``) before ``park`` runs; ``park`` itself writes
    its reason comment only when it lands (``_commented``). [daveey]"""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_wake_check as kwc

    def run(tid):
        wake_at = kwc.planned_wake(conn, tid, op=op, **plan)
        err = kwc.guard_error(wake_at=wake_at, wake_check=wake[0], clock_only=wake[2])
        if err:
            refused[tid] = f"cannot {op} {tid}: {err}"
            return False
        try:
            if wake_at is not None:
                kaw.resolve(conn, tid, awaiting=awaiting, reason=plan.get("reason"), wake_check=wake[0],
                            clock_only=wake[2], op=op)
            return park(tid)
        except kaw.AwaitingError as exc:
            refused[tid] = f"cannot {op} {tid}: {exc}"
            return False
        except ValueError as exc:   # e.g. a wake_check on a block that is not a timed park
            refused[tid] = f"cannot {op} {tid}: {exc}"
            return False
    return run


def _parked_msg(conn, verb: str, suffix: str):
    """``ok_msg`` for a park that may have escalated to an owner card instead."""
    from hermes_cli import kanban_awaiting as kaw

    def msg(tid):
        owner = kaw.escalated_owner(conn, tid)
        return kaw.escalation_message(tid, owner) if owner else f"{verb} {tid}{suffix}"
    return msg
