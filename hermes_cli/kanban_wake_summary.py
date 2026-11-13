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
    """``{"check", "next_probe_at", "backstop_at", "blocks_for_human"}`` for a scheduled card, else None.

    ``check`` is the armed wake-check command (None for a pure clock park); ``backstop_at`` is
    the park's deadline. The park is read through ``kanban_continuation.park_state``, the same
    lookup the dispatcher wakes on. ``blocks_for_human``: a legacy park with no timed wake and a
    human ``block_kind``, which the dispatcher's wake invariant moves to ``blocked`` on its next
    tick instead of waking (``backstop_at`` is then None). Pass ``wake_check`` with
    ``check_loaded=True`` when the caller already read ``kanban_wake_check.get_state``."""
    if getattr(task, "status", None) != "scheduled":
        return None
    from hermes_cli.kanban_continuation import blocks_for_human, park_state
    from hermes_cli.kanban_timed_park import implied_wake

    if not check_loaded:
        from hermes_cli import kanban_wake_check as kwc
        wake_check = kwc.get_state(conn, task.id)
    if wake_check and wake_check.get("purpose") == "ask_resolved":
        wake_check = None  # a human ask's resolved_check, not this park's wake
    park = park_state(conn, task.id)
    human = blocks_for_human(park, getattr(task, "block_kind", None))
    return {
        "check": wake_check.get("cmd") if wake_check else None,
        "next_probe_at": int(wake_check["next_at"]) if wake_check and wake_check.get("next_at") is not None else None,
        "backstop_at": None if human else implied_wake(park["payload"], park["parked_at"]),
        "blocks_for_human": human,
    }


HUMAN_BLOCK_PHRASE = "no timed wake: the dispatcher blocks it for a human on its next tick"


def phrase(w: Optional[dict], *, now: Optional[int] = None) -> str:
    """``wakes when `cmd` passes (next probe HH:MM), backstop YYYY-MM-DD HH:MM`` or ``wakes at …``;
    empty for None. The deadline is called a backstop only when a check can wake the card first.
    A park the dispatcher will block for a human says so instead of naming a wake."""
    if not w:
        return ""
    if w.get("blocks_for_human"):
        return HUMAN_BLOCK_PHRASE
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
