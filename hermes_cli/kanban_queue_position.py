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
    """Unclaimed ready cards of the same assignee the dispatcher reaches first (higher priority, or the same
    priority and created earlier). Worker-first cards wait for the pool, not a local slot, so they are not ahead."""
    from hermes_cli.kanban_worker_placement import POLICY
    return int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'ready' AND claim_lock IS NULL AND id != ? AND assignee IS ? "
        "AND placement_policy IS NOT ? AND (priority > ? OR (priority = ? AND created_at < ?))",
        (task.id, task.assignee, POLICY, int(task.priority or 0), int(task.priority or 0), int(task.created_at or 0)),
    ).fetchone()[0])


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
    running, limit = _running_and_limit(conn, board)
    return {"lane": "local", "ahead": _ahead(conn, task), "running": running, "limit": limit, "approximate": True}
