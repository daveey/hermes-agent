"""Pickup budget for foreign cards the Board already shows held by a respawn guard. [daveey]

A dispatcher that holds a card (``active_pr``, ``subscription_quota_reset``, cooldowns, ...) records a
``respawn_guarded`` event, and the event travels to the Board with the card's history, so the replica shows the hold
before this machine copies the card. Such a card does not take the pickup budget: the budget is for cards a slot
would start. worker-1 2026-11-12 11:40-11:57Z: four guarded hub cards at priority 360..20801 filled the 3-card batch
on every tick (49 imports in 17 min), and six cards pinned to ``machine:worker-1`` waited ``budget_exhausted``
(t_01a1cf9fa5c6616f).

The card is still copied, outside the budget (at most :data:`FREE_PER_TICK` per tick), because the guard is
decided where the card would run: a quota or PR hold can lift, and only a dispatcher that holds a copy re-checks it.
A copy the local dispatcher then holds is kept (``kanban_worker_node._guard_held``). One it does not hold is
evicted, and is not copied again for :data:`RECHECK_SECONDS` unless its Board hold changes (new hold start or a
task row change), so an unkept copy is not re-imported every tick.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

FREE_PER_TICK = 4
RECHECK_SECONDS = 600

# task id -> (hold signature, monotonic time it was copied outside the budget). Process-local: a restart re-checks.
_COPIED: dict[str, tuple[tuple, float]] = {}


def board_hold(replica: sqlite3.Connection, card: sqlite3.Row) -> Optional[tuple[str, tuple]]:
    """``(guard reason, signature)`` when the card's Board history ends in an unbroken ``respawn_guarded`` run,
    else None. Read-only. A failed read is None: the card then takes the budget as before."""
    from hermes_cli.kanban_db_dispatch_guard_notice import guard_hold_reason, guard_hold_started_at
    try:
        reason = guard_hold_reason(replica, card["id"], include_replies=False)
        if reason is None:
            return None
        version = card["sync_version"] if "sync_version" in card.keys() else None
        return reason, (version, guard_hold_started_at(replica, card["id"]), reason)
    except Exception:  # health: allow BLE001 -- advisory; the local guard and the Board lease still decide
        _log.warning("kanban worker node: could not read the Board hold of %s", card["id"], exc_info=True)
        return None


def recently_copied(tid: str, signature: tuple, *, now: Optional[float] = None) -> bool:
    """True when this card was copied outside the budget under the same hold within :data:`RECHECK_SECONDS`."""
    seen = _COPIED.get(tid)
    now = time.monotonic() if now is None else now
    return seen is not None and seen[0] == signature and now - seen[1] < RECHECK_SECONDS


def remember(copied: dict[str, tuple], *, now: Optional[float] = None) -> None:
    """Record the cards copied outside the budget this tick; forget entries past the recheck window."""
    now = time.monotonic() if now is None else now
    for tid in [t for t, (_, at) in _COPIED.items() if now - at >= RECHECK_SECONDS]:
        del _COPIED[tid]
    for tid, signature in copied.items():
        _COPIED[tid] = (signature, now)


def reset() -> None:
    _COPIED.clear()
