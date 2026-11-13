"""What wakes a ``scheduled`` card, said in one place. [daveey]

A scheduled card wakes on whichever comes first: its park's wake check passing, or the park's
deadline (``wake_at``, or the deadline ``kanban_timed_park.implied_wake`` derives for older
parks). ``kanban show`` used to print ``status: scheduled`` and the check in a separate field
below Diagnostics, while the raw ``SCHEDULED_UNTIL=…`` sat in the run line; an agent that
grepped ``status:`` quoted the deadline as the wake time when it was only the backstop
(defect agent-3a632ca054). Every card-status surface (CLI status line, ``--json`` ``wakes``,
worker_context ``Wakes:``) renders from :func:`wakes`.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional


def _stamp(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return time.strftime(fmt, time.localtime(ts))


def wakes(conn: sqlite3.Connection, task: Any, *, wake_check: Optional[dict] = None,
          check_loaded: bool = False) -> Optional[dict]:
    """``{"check", "next_probe_at", "backstop_at"}`` for a scheduled card, else None.

    ``check`` is the armed wake-check command (None for a pure clock park); ``backstop_at`` is
    the park's deadline. Pass ``wake_check`` with ``check_loaded=True`` when the caller already
    read ``kanban_wake_check.get_state``."""
    if getattr(task, "status", None) != "scheduled":
        return None
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import SCHEDULED_ENTRY_EVENTS
    from hermes_cli.kanban_timed_park import implied_wake

    if not check_loaded:
        from hermes_cli import kanban_wake_check as kwc
        wake_check = kwc.get_state(conn, task.id)
    if wake_check and wake_check.get("purpose") == "ask_resolved":
        wake_check = None  # a human ask's resolved_check, not this park's wake
    marks = ",".join("?" for _ in SCHEDULED_ENTRY_EVENTS)
    row = conn.execute(
        f"SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (task.id, *SCHEDULED_ENTRY_EVENTS)).fetchone()
    if row is not None:
        payload, parked_at = kb._json_dict(row[0]), int(row[1])
    else:
        last = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ?", (task.id,)).fetchone()
        payload, parked_at = {}, int(last[0]) if last and last[0] else int(time.time())
    return {
        "check": wake_check.get("cmd") if wake_check else None,
        "next_probe_at": int(wake_check["next_at"]) if wake_check and wake_check.get("next_at") is not None else None,
        "backstop_at": implied_wake(payload, parked_at),
    }


def phrase(w: Optional[dict], *, now: Optional[int] = None) -> str:
    """``wakes when `cmd` passes (next probe HH:MM), backstop YYYY-MM-DD HH:MM`` or ``wakes at …``;
    empty for None. The deadline is called a backstop only when a check can wake the card first."""
    if not w:
        return ""
    now = int(time.time()) if now is None else int(now)
    if not w.get("check"):
        return f"wakes at {_stamp(w['backstop_at'])}"
    nxt = w.get("next_probe_at")
    probe = ("next probe due now" if nxt is None or nxt <= now else f"next probe {_stamp(nxt, '%H:%M')}")
    return f"wakes when `{w['check']}` passes ({probe}), backstop {_stamp(w['backstop_at'])}"


def status_text(status: str, w: Optional[dict]) -> str:
    """The ``status:`` value: the status word first (``awk '{print $2}'`` parsers), then the wake."""
    text = phrase(w)
    return f"{status} — {text}" if text else status


def context_line(conn: sqlite3.Connection, task: Any) -> Optional[str]:
    """worker_context's ``Wakes:`` header line; None when the card is not scheduled. Advisory: a
    read failure leaves the line out rather than breaking context assembly."""
    try:
        text = phrase(wakes(conn, task))
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return None
    return f"Wakes:    {text.removeprefix('wakes ')}" if text else None
