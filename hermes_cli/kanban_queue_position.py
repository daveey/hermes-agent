"""Where a ``ready`` card stands in the queue that will start it, for task reads. [daveey]

A ready card with no claim for hours looked the same as a stuck one, so chats re-investigated queued cards
(three times in a day, 2026-11-02/03). The hub's own dispatcher claims ready cards by priority, then age
(``kanban_db_dispatch_queries.lane_rows``), under a host concurrency cap; a ``worker-first-v1`` card waits for the
worker pool instead, and the Board's last answer about it is stored by ``kanban_queue_reason``.

:func:`position` returns ``{"lane": "local", "ahead", "running", "limit", "approximate": True}`` or
``{"lane": "worker_pool", "reason": <code>}``; None for a card that is not ready.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

_log = logging.getLogger(__name__)


def _ahead(conn: sqlite3.Connection, task) -> int:
    """Unclaimed ready cards of the same assignee the dispatcher reaches first, in its own lane order
    (``DISPATCH_ORDER``: effective priority, then cards others wait on, then age). Worker-first cards wait for the
    pool, not a local slot, so they are not ahead."""
    from hermes_cli.kanban_db_dispatch_queries import DISPATCH_ORDER
    from hermes_cli.kanban_worker_placement import POLICY
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND claim_lock IS NULL AND assignee IS ? "
        f"AND (placement_policy IS NOT ? OR id = ?) ORDER BY {DISPATCH_ORDER}",
        (task.assignee, POLICY, task.id),
    ).fetchall()]
    return ids.index(task.id) if task.id in ids else len(ids)


def _running_and_limit(conn: sqlite3.Connection, board: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """The host's running workers (every board) and its ``max_in_progress`` cap, as the dispatcher counts them."""
    from hermes_cli import kanban_db_dispatch as kbd
    try:
        running = kbd.count_running_tasks(conn) + kbd.count_running_tasks_other_boards(board)
    except Exception:  # health: allow BLE001 -- an unreadable count is unknown, not zero
        _log.debug("kanban queue position: running count unavailable", exc_info=True)
        running = None
    try:
        limit = kbd.resolve_max_in_progress(kbd.configured_max_in_progress())
    except Exception:  # health: allow BLE001 -- an unreadable cap is unknown
        _log.debug("kanban queue position: cap unavailable", exc_info=True)
        limit = None
    return running, limit


def _runs_on(conn: sqlite3.Connection, task_id: str) -> Optional[list[str]]:
    try:
        row = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:  # an unsynced schema has no runs_on column: no placement labels
        return None
    from hermes_cli.kanban_worker_node import _parse
    return _parse(row[0]) if row is not None else None


def placement(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """For a card whose ``runs_on`` this machine does not satisfy, on a synced board with a readable machine
    registry: ``{"lane": "unplaceable", runs_on, missing, machines}`` when no known machine satisfies it (``missing``
    = labels no machine carries; empty when each exists but none carries them all), else ``{"lane": "machine",
    runs_on, machines}`` naming the machines that can lease it. None otherwise (no labels, sync off, registry
    unknown, or this machine fits): an unknown registry never reads as unplaceable. [daveey] t_01a1a521638d0a78
    (runs_on ["hermes-hub"], no machine carries it) sat ready for days reading as a local slot wait."""
    labels = _runs_on(conn, task_id)
    if not labels:
        return None
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as kwn
    db = sync._db_key(conn)
    if not sync.sync_enabled() or not db or not sync.board_selected(db):
        return None
    try:
        reg = kwn.registry()
    except Exception:  # health: allow BLE001 -- an unreadable registry is unknown, never "unplaceable"
        _log.debug("kanban queue position: machine registry unavailable", exc_info=True)
        return None
    machines = (reg or {}).get("machines") or {}
    me = (reg or {}).get("me")
    if not machines or me not in machines:
        return None
    carried = {name: kwn.effective_labels(entry, name) for name, entry in machines.items()}
    wanted = set(labels)
    if wanted <= carried[me]:
        return None
    fits = sorted(name for name, have in carried.items() if wanted <= have)
    if fits:
        return {"lane": "machine", "runs_on": labels, "machines": fits}
    every = set().union(*carried.values())
    return {"lane": "unplaceable", "runs_on": labels, "missing": [lb for lb in labels if lb not in every],
            "machines": sorted(machines)}


def position(conn: sqlite3.Connection, task, board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The queue a ``ready`` ``task`` in this gateway's live file waits in; None for any other status."""
    if task is None or task.status != "ready":
        return None
    from hermes_cli.kanban_worker_placement import POLICY
    if getattr(task, "placement_policy", None) == POLICY:
        from hermes_cli import kanban_queue_reason as kqr
        try:
            value = kqr.latest(conn, task.id)
        except (sqlite3.Error, IndexError):  # an unsynced schema has no lease columns: no stored reason
            value = None
        return {"lane": "worker_pool", "reason": (value or {}).get("code")}
    placed = placement(conn, task.id)
    if placed is not None:
        return placed
    running, limit = _running_and_limit(conn, board)
    return {"lane": "local", "ahead": _ahead(conn, task), "running": running, "limit": limit, "approximate": True}


_NOT_STUCK = ("queued, not stuck: the dispatcher starts it; do not run its work outside the dispatcher "
              "(two workers on one card). To start it sooner, raise its priority (`hermes kanban edit <id> "
              "--priority 10`)")


def describe(place: Optional[dict[str, Any]]) -> Optional[str]:
    """One line for card reads (``kanban_show`` ``queue_note``, ``hermes kanban show``) saying what the card waits
    for, so "ready, no run" never reads as stuck. [daveey] kanban-7d720d4ab8: a chat saw only a null
    ``queue_reason`` for a card waiting on a full hub, called it stuck and ran its work in a subagent."""
    if not place:
        return None
    lane = place.get("lane")
    if lane == "unplaceable":
        missing = place.get("missing") or []
        what = (f"label(s) {', '.join(missing)} carried by no machine" if missing
                else "no machine carries all of " + ", ".join(place.get("runs_on") or []))
        return (f"unplaceable: no machine can run it ({what}; known: {', '.join(place.get('machines') or [])}). "
                "Re-place it (`hermes kanban move <id> --runs-on <label> --reason ...`); waiting will not start it")
    if lane == "machine":
        return (f"waiting for {', '.join(place.get('machines') or [])} (runs_on "
                f"{', '.join(place.get('runs_on') or [])}); this machine cannot lease it. " + _NOT_STUCK)
    if lane == "worker_pool":
        return f"waiting for a pool worker ({place.get('reason') or 'no reason recorded'}). " + _NOT_STUCK
    ahead = place.get("ahead")
    words = f"waiting for a local slot: ~{ahead} ahead" if ahead is not None else "waiting for a local slot"
    if place.get("running") is not None and place.get("limit") is not None:
        words += f", {place['running']}/{place['limit']} running"
    return words + ". " + _NOT_STUCK


def read(conn: sqlite3.Connection, task, board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """:func:`position` for card reads (``kanban_show``, ``hermes kanban show``): advisory, never fails the read."""
    try:
        return position(conn, task, board)
    except Exception:  # health: allow BLE001 -- an advisory queue place must never fail the read
        _log.debug("kanban queue position: unavailable for %s", getattr(task, "id", None), exc_info=True)
        return None


def fields(place: Optional[dict[str, Any]]) -> dict[str, Any]:
    """``{"queue", "queue_note"}`` for a card-read payload; empty when the card waits in no queue."""
    return {"queue": place, "queue_note": describe(place)} if place else {}


def print_field(place: Optional[dict[str, Any]], queue_reason: Any, field) -> None:
    """The text ``queue:`` line, unless the worker-pool ``queue_reason`` line already took it."""
    if place and not queue_reason:
        field("queue", describe(place))
