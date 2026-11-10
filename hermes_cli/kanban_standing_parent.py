"""Refuse a never-finishing "standing" card as a dependency parent. [daveey]

A standing card (a permanent listener: "Standing: support Eve", a hand-off watcher) re-parks itself forever
and never reaches ``done``, so a child linked under it waits forever while every health check sees a validly
scheduled parent (defect agent-0fddefc2d0: the Ask Eve rollout card sat in ``todo`` for 8 days). A wait on
an event is a ``kanban_schedule`` park with a ``wake_check``; a wait on a person is a ``human_request`` block.

Standing = not done/archived, and either the title starts with "Standing:" or the card ended more than
``REPARK_LIMIT`` runs as ``scheduled`` in the last ``REPARK_WINDOW_S`` seconds (an active re-park cycle with
no completion in that window).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

REPARK_LIMIT = 5
REPARK_WINDOW_S = 72 * 3600
TITLE_PREFIX = "standing:"


def standing_reason(conn: sqlite3.Connection, task_id: str, *, now: Optional[float] = None) -> Optional[str]:
    """Why ``task_id`` is a standing card, or None when it is an ordinary (finishable) parent."""
    row = conn.execute("SELECT title, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] in ("done", "archived"):
        return None
    if (row["title"] or "").strip().lower().startswith(TITLE_PREFIX):
        return "its title starts with 'Standing:'"
    since = int((now if now is not None else time.time()) - REPARK_WINDOW_S)
    parks = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND outcome = 'scheduled' AND ended_at >= ?",
        (task_id, since),
    ).fetchone()[0]
    if parks > REPARK_LIMIT:
        return f"it re-parked {parks} times in the last 72 h without completing"
    return None


def standing_parent_error(parent_id: str, child_id: str, reason: str) -> ValueError:
    return ValueError(
        f"cannot link {parent_id} -> {child_id}: {parent_id} is a standing card ({reason}); it never "
        "finishes, so the child would wait forever. Wait on an event with kanban_schedule(wake_check=...) "
        "(a probe on the reply/file/row you expect; it sends nothing), or on a person with "
        "kanban_block(kind='needs_input', human_request={...}). Wakes are not messages."
    )
