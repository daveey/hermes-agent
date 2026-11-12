"""One Kanban event, one turn, when a chat holds both a ``tui`` and a waking ``local`` row for a card. [daveey]

A Desktop chat on the canonical gateway subscribes to its cards twice: a ``local`` row with
``delivery_mode='notify+wake'`` (the gateway notifier wakes the chat; the LOCAL adapter's text ping is a
no-op) and a ``tui`` ``notify`` row (the legacy Desktop poller and the unloaded sweeper write a ``kanban_notice``
row and start a turn). Each row has its own cursor, so one completion started two model turns minutes apart
(t_01a1ce9070eee14c: messages 872084 and 872153 for t_01a1ce83e793e24f).

The waking ``local`` row owns the turn for every event it wakes on. The ``tui`` consumers treat those events as
silent (claimed and acked, no row, no turn), in whichever order the two legs run, and the wake turn carries the
notice line the ``tui`` row would have shown (:func:`notice_lines`). Kinds the wake leg does not wake on
(``status``, quota and re-pin notices) stay with the ``tui`` row. A ``notify``-only ``local`` row covers nothing.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

WAKE_MODES = ("notify+wake", "wake")


def _sibling(conn: Any, sub: dict, platform: str, modes: Optional[tuple] = None) -> bool:
    sql = ("SELECT 1 FROM kanban_notify_subs WHERE task_id = ? AND platform = ? AND chat_id = ? "
           "AND thread_id = ?")
    args: list = [sub["task_id"], platform, sub["chat_id"], sub.get("thread_id") or ""]
    if modes:
        sql += f" AND delivery_mode IN ({','.join('?' * len(modes))})"
        args.extend(modes)
    try:
        return conn.execute(sql + " LIMIT 1", args).fetchone() is not None
    except sqlite3.Error:
        return False  # unreadable: deliver as before (a duplicate beats a lost notice)


def covered(ev: Any) -> bool:
    """The waking ``local`` leg starts a turn for this event (``_WAKE_KINDS``, not a provisional failure)."""
    from gateway.kanban_watchers_notifier import _WAKE_KINDS
    from hermes_cli.kanban_failure_notice import provisional_failure
    return getattr(ev, "kind", "") in _WAKE_KINDS and not provisional_failure(ev)


def tui_cover(conn: Any, sub: dict) -> Optional[Callable[[Any], bool]]:
    """For a ``tui`` row: :func:`covered` when a waking ``local`` row for the same card and chat delivers its
    events, else None (deliver everything as before)."""
    if (sub.get("platform") or "").lower() != "tui":
        return None
    return covered if _sibling(conn, sub, "local", WAKE_MODES) else None


def tui_formatter(conn: Any, sub: dict, fmt: Callable) -> Callable:
    """``fmt`` (``(sub, task, ev, board) -> text | None``) with the events :func:`tui_cover` covers silenced."""
    cover = tui_cover(conn, sub)
    if cover is None:
        return fmt

    def silenced(s, task, ev, board):
        return None if cover(ev) else fmt(s, task, ev, board)
    return silenced


def has_tui_sibling(conn: Any, sub: dict) -> bool:
    """For a waking ``local`` row: a ``tui`` row for the same card and chat defers its notice to this wake."""
    if (sub.get("platform") or "").lower() != "local" or (sub.get("delivery_mode") or "notify") not in WAKE_MODES:
        return False
    return _sibling(conn, sub, "tui")


def notice_lines(sub: dict, task: Any, events, board_slug: Optional[str]) -> list[str]:
    """The notice text the deferring ``tui`` row would have written for the covered events, for the wake turn."""
    from tui_gateway.session_notifications import _format_kanban_event_text
    lines = []
    for ev in events:
        if covered(ev) and (text := _format_kanban_event_text(sub, task, ev, board_slug or "")):
            lines.append(text)
    return lines
