"""Post-commit outcome refresh for a card's origin and shepherd chats.

[daveey] A chat's stored ``sessions.outcome_json`` used to refresh only on chat triggers (turn end, reap, notice,
mute) and from the card-change poller (``tui_gateway.notice_fanout``), so a card block/unblock/complete/archive left
the stored outcome stale until the next poll tick: the stripe kept painting a withdrawn ask (or missed a new one).
``refreshes_origin_outcomes`` wraps those four transitions; after the transition commits (never inside a caller's
open transaction) it refreshes each linked chat's outcome with the same ``card_change`` trigger the poller uses.

The refresh runs only inside a process that owns chat sessions (the TUI gateway has ``tui_gateway.server`` loaded):
a worker or CLI process leaves it to the gateway's poller. It is best-effort and off-thread: a failure is logged and
never changes the transition's result.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import sys
from typing import Any, Callable

logger = logging.getLogger(__name__)


def linked_chats(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """The chats whose row counts this card: its creating chat and each ``tui`` subscriber that owns its progress
    (``create``/``shepherd`` — a touch-only subscriber does not), minus released links."""
    from hermes_cli.kanban_session_activity import _counted_sub_clause

    chats: list[str] = []
    row = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is not None and row[0]:
        chats.append(str(row[0]))
    try:
        chats += [str(r[0]) for r in conn.execute(
            f"SELECT s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id WHERE s.task_id = ? "
            f"AND LOWER(s.platform) = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)}", (task_id,))]
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    released: set[str] = set()
    try:
        released = {str(r[0]) for r in conn.execute(
            "SELECT chat_session, MIN(released) FROM task_chat_links WHERE task_id = ? GROUP BY chat_session",
            (task_id,)) if r[1]}
    except sqlite3.Error:  # boards that never synced have no link table
        pass
    return [c for c in dict.fromkeys(chats) if c not in released]


def refresh_chats(chats: list[str]) -> int:
    """Refresh each chat's stored outcome (trigger ``card_change``); returns how many refreshed. Never raises."""
    done = 0
    for chat in chats:
        try:
            from hermes_state_outcome import refresh_outcomes_quietly
            from tui_gateway import kanban_unloaded as ku
            from tui_gateway.notice_fanout import chat_skip_reason

            with ku.find_session_db(chat) as (db, tip):
                if db is None or chat_skip_reason(db, chat) is not None:
                    continue
                done += refresh_outcomes_quietly([tip or chat], "card_change", db=db)
        except Exception:  # health: allow BLE001 -- best-effort, the poller is the backstop
            logger.warning("card-transition outcome refresh failed for %s", chat, exc_info=True)
    return done


def owns_chat_sessions() -> bool:
    """True in a process that owns chat sessions (the TUI gateway has ``tui_gateway.server`` loaded)."""
    return "tui_gateway.server" in sys.modules


def after_transition(conn: sqlite3.Connection, task_id: str) -> None:
    """Refresh the card's linked chats off-thread. Skips when the caller still holds a transaction (the state would
    not be durable yet) and in processes that own no chat sessions."""
    try:
        if conn.in_transaction or not owns_chat_sessions():
            return
        chats = linked_chats(conn, task_id)
        if chats:
            from agent.memory_provider import spawn_context_thread

            spawn_context_thread(refresh_chats, name="card-transition-outcome", args=(chats,)).start()
    except Exception:  # health: allow BLE001 -- never break a transition
        logger.debug("card-transition outcome refresh skipped for %s", task_id, exc_info=True)


def refreshes_origin_outcomes(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for ``(conn, task_id, ...)`` transitions: after a truthy result, :func:`after_transition`."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if result:
            conn = args[0] if args else kwargs.get("conn")
            task_id = args[1] if len(args) > 1 else kwargs.get("task_id")
            if conn is not None and task_id:
                after_transition(conn, str(task_id))
        return result

    wrapper._refreshes_outcomes = True  # type: ignore[attr-defined]
    return wrapper
