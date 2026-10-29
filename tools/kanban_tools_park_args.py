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


def _clear_blocker_arg(args: dict, blocker_id: Optional[str] = None) -> bool:
    """Validated ``clear_blocker``: the caller saw the card's shared blocker gone. [daveey]"""
    raw = args.get("clear_blocker", False)
    _check(isinstance(raw, bool), "clear_blocker must be true or false")
    _check(not (raw and blocker_id), "clear_blocker says the wall is gone; blocker_id says it still stands. Pass one")
    return raw
