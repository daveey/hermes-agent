"""Argument validators for park calls (kanban_block / kanban_schedule / kanban_continue). [daveey]

Split out of tools/kanban_tools.py to keep that facade under its size cap; behaviour unchanged.
"""
from __future__ import annotations

from typing import Any, Optional


def _check(cond: Any, message: str, rejection: Optional[str] = None) -> None:
    from tools.kanban_tools import _check as check  # lazy: kanban_tools imports this module

    check(cond, message, rejection)


def _blocker_id_arg(args: dict) -> Optional[str]:
    """Validated ``blocker_id`` (shared external blocker slug) or None. [daveey]"""
    raw = args.get("blocker_id")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    _check(isinstance(raw, str), "blocker_id must be a string")
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id
    bid = normalize_blocker_id(raw)
    _check(bid is not None, "blocker_id must be a short slug like 'github-actions-billing'")
    return bid


def _blocker_scope_args(args: dict) -> dict:
    from hermes_cli.kanban_blocker_metadata import scope_fields
    try:
        return scope_fields(args)
    except ValueError as exc:
        _check(False, str(exc))
        return {}


def _clear_blocker_arg(args: dict, blocker_id: Optional[str] = None) -> bool:
    """Validated ``clear_blocker``: the caller saw the card's shared blocker gone. [daveey]"""
    raw = args.get("clear_blocker", False)
    _check(isinstance(raw, bool), "clear_blocker must be true or false")
    _check(not (raw and blocker_id), "clear_blocker says the wall is gone; blocker_id says it still stands. Pass one")
    return raw


def _park_guard(conn, tid: str, op: str, wake: tuple, **plan: Any) -> None:
    """Refuse a park that wakes > 2 h away with no wake_check and no clock_only. [daveey]"""
    from hermes_cli import kanban_wake_check as kwc

    wake_at = kwc.planned_wake(conn, tid, op=op, **plan, **({"wake_check": wake[0], "clock_only": wake[2]}
                                                            if op == "block" else {}))
    err = kwc.guard_error(wake_at=wake_at, wake_check=wake[0], clock_only=wake[2])
    _check(err is None, f"kanban_{op} {err}", "invalid_args")


def _wake_check_args(args: dict) -> tuple[Optional[str], Optional[int], bool]:
    """``(wake_check, check_every, clock_only)`` from a park call, validated. [daveey]"""
    from hermes_cli import kanban_wake_check as kwc

    raw_every = args.get("check_every")
    _check(raw_every is None or (isinstance(raw_every, (int, float)) and not isinstance(raw_every, bool)),
           "check_every must be an integer number of seconds")
    clock_only = args.get("clock_only", False)
    _check(isinstance(clock_only, bool), "clock_only must be true or false")
    try:
        cmd = kwc.normalize_cmd(args.get("wake_check"))
        every = kwc.normalize_every(raw_every) if cmd else None
        err = kwc.dry_run_error(cmd, blocker_id=args.get("blocker_id"))
        if err:
            raise ValueError(err)
    except ValueError as exc:
        _check(False, str(exc), kwc.refusal_code(str(exc)))
    from tools.kanban_tools import _redact  # lazy: kanban_tools imports this module
    return (_redact(cmd) if cmd else None), every, clock_only


def _wake_check_echo(wake: tuple) -> dict:
    """Result fields echoing the park's probe (none when it has no probe)."""
    return {"wake_check": wake[0], "check_every": wake[1]} if wake[0] else {}
