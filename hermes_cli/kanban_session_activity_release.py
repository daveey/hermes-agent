"""Release rule for remote cards: the owner itself, recently, published the card as closed. [daveey]

Moved out of ``kanban_session_activity`` (file-size cap); behaviour unchanged.
"""

from __future__ import annotations

from typing import Any

from hermes_cli import kanban_scope_heartbeat as _scope_hb


TERMINAL_STATUSES = ("done", "archived")


def owner_attests_terminal(task: dict[str, Any], gw: str, owner_profile: str | None, board: str,
                           heartbeats: dict[tuple[str, str, str], tuple[float, float]], now: float, stale: bool,
                           evidence: dict[str, Any]) -> bool:
    """The owner itself, recently, published this card as closed.

    Release needs all of: a fresh hub sync; a profile-qualified record whose own identity fields match its
    key; a terminal status inside the owner collector's hash-bound observation (``origin_observation``, so a
    replica row edited or truncated in transit proves nothing); and that collector's per-scope heartbeat for
    this exact (profile, board) within that owner's window (``kanban_scope_heartbeat``), so an offline owner's last word is not taken
    as current. Absence of a row is never release. This is the same rule a local board applies to its own
    cards (``done``/``archived`` are not counted), applied only to evidence the owner signed. Pre-ledger
    cards had no other release path, so every one of them stayed open forever. ``evidence`` is the task's
    ``_owner_evidence`` (``_decode_replica_task``). [daveey]
    """
    if stale or owner_profile is None or task.get('status') not in TERMINAL_STATUSES:
        return False
    if task.get('profile') != owner_profile or task.get('board') != board or task.get('gateway') != gw:
        return False
    if not evidence:
        return False
    # A card bound through the origin ledger keeps the ledger's stricter release: its owner must
    # have verified the completion (lifecycle event, run, contract and goal receipts), not just closed it.
    # An archive is the exception: it produces nothing, so it closes the link without counting as completed
    # (``closed_uncompleted`` on the row). [daveey]
    if task.get('origin_bindings') and evidence.get('verified') is not True and task.get('status') != 'archived':
        return False
    beat = heartbeats.get((gw, owner_profile, board))
    return beat is not None and _scope_hb.fresh(beat[0], now, beat[1])
