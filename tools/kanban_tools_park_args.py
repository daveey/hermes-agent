"""Argument validators and result helpers for park calls. [daveey]

Split out of tools/kanban_tools.py to keep that facade under its size cap; behaviour unchanged.
"""
from __future__ import annotations

from typing import Any, Optional

from hermes_cli import kanban_wake_check_secret


def _check(cond: Any, message: str, rejection: Optional[str] = None) -> None:
    from tools.kanban_tools import _check as check  # lazy: kanban_tools imports this module

    check(cond, message, rejection)


def _awaiting_arg(args: dict, wake: tuple) -> tuple[Optional[dict], tuple]:
    """``(awaiting, wake)``: declared producer and the park's optional card probe."""
    import os
    from tools.kanban_tools import _Reject
    from hermes_cli import kanban_awaiting as kaw

    try:
        awaiting = kaw.normalize(args.get("awaiting"))
    except kaw.AwaitingError as exc:
        raise _Reject(str(exc), exc.code) from exc
    if awaiting and awaiting["kind"] == "human":
        exc = kaw.human_error(os.environ.get("HERMES_KANBAN_TASK") or "",
                              " ".join(str(args.get(k) or "") for k in ("reason", "checkpoint")) + " "
                              + str(awaiting.get("why") or ""))
        raise _Reject(str(exc), exc.code)
    probe = None if wake[0] else kaw.card_probe(awaiting)
    if probe:
        from hermes_cli import kanban_wake_check as kwc
        wake = (probe, kwc.normalize_every(None), wake[2])
    elif not wake[0] and kaw.declared_chat_wait(awaiting):
        wake = (wake[0], wake[1], True)  # its comment probe is added in the park txn (kanban_awaiting.park_probe)
    try:
        kaw.confirm_remote(awaiting, hint=" ".join(str(x) for x in (wake[0], args.get("reason"), args.get("checkpoint")) if x))
    except kaw.AwaitingError as exc:
        raise _Reject(str(exc), exc.code) from exc
    return awaiting, wake


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


def _wake_check_args(args: dict, *, conn=None, tid: Optional[str] = None) -> tuple[Optional[str], Optional[int], bool]:
    """``(wake_check, check_every, clock_only)`` from a park call, validated. With ``conn``/``tid``, a probe the hub
    runs for this card (``kanban_wake_check_hub_relay``) skips the dry run here: it would read this board. [daveey]"""
    from hermes_cli import kanban_wake_check as kwc
    from hermes_cli.kanban_wake_check_hub_relay import relay_host

    raw_every = args.get("check_every")
    _check(raw_every is None or (isinstance(raw_every, (int, float)) and not isinstance(raw_every, bool)),
           "check_every must be an integer number of seconds")
    clock_only = args.get("clock_only", False)
    _check(isinstance(clock_only, bool), "clock_only must be true or false")
    try:
        cmd = kwc.normalize_cmd(args.get("wake_check"))
        every = kwc.normalize_every(raw_every) if cmd else None
    except ValueError as exc:
        _check(False, str(exc))
        raise  # unreachable: _check raised
    secret = _wake_check_secret_error(cmd) if cmd else None
    _check(secret is None, secret or "", WAKE_CHECK_SECRET)  # before the dry run executes it
    try:
        err = None if relay_host(conn, tid, cmd) else kwc.dry_run_error(cmd, blocker_id=args.get("blocker_id"))
        if err:
            raise ValueError(err)
    except ValueError as exc:
        _check(False, str(exc), kwc.refusal_code(str(exc)))
    return cmd, every, clock_only  # verbatim: the stored probe is the command that runs


WAKE_CHECK_SECRET = kanban_wake_check_secret.WAKE_CHECK_SECRET


def _wake_check_secret_error(cmd: str) -> Optional[str]:
    """Refusal text when the display redactor would change ``cmd``, else None. [daveey]

    Shared with ``hermes kanban block/schedule --wake-check`` (``hermes_cli/kanban_wake_check_secret``)."""
    return kanban_wake_check_secret.secret_error(cmd)


def _wake_check_echo(wake: tuple) -> dict:
    """Result fields echoing the park's probe (none when it has no probe)."""
    return {"wake_check": wake[0], "check_every": wake[1]} if wake[0] else {}


def _escalated(kb, conn, tid: str) -> Optional[str]:
    """The result for a park that escalated to an owner card instead (``kanban_awaiting``). [daveey]"""
    from hermes_cli import kanban_awaiting as kaw
    from tools.kanban_tools import _ok_landed

    owner = kaw.escalated_owner(conn, tid)
    if owner is None:
        return None
    return _ok_landed(kb, conn, tid, "todo", code="repark_escalated", owner_task_id=owner,
                      next=kaw.escalation_message(tid, owner, kaw.escalated_fired(conn, tid)))
