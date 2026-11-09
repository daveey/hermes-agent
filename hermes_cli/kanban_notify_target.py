"""Say which chat a ``notify-subscribe --platform tui`` binds, and warn on a different chat. [daveey]

An agent once ran the subscribe with another chat's id (a hint read the process-global session env), read back the
subscriber ids and reported its own chat linked (agent-884fdd96f8). The command now prints the target chat's
title, and a WARNING when the calling agent's chat is a different one.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional


def _title_and_tip(session_id: str) -> tuple[Optional[str], Optional[str], bool]:
    """``(title, compression tip, found)`` of a chat in this profile's state.db; ``(None, None, False)`` when it
    is not there or the db cannot be read."""
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
        db = SessionDB(db_path=get_hermes_home() / "state.db", read_only=True)
    except (ImportError, OSError, sqlite3.Error):
        return None, None, False
    try:
        row = db.get_session(session_id)
        if not row:
            return None, None, False
        try:
            tip = db.get_compression_tip(session_id) or session_id
        except sqlite3.Error:
            tip = session_id
        return (str(row.get("title") or "").strip() or None), tip, True
    except sqlite3.Error:
        return None, None, False
    finally:
        db.close()


def print_tui_target(task_id: str, chat_id: str) -> None:
    """Print the bound chat's title; warn on stderr when it is not the calling agent's own chat.

    The subprocess env names the calling chat: ``HERMES_SESSION_KEY`` (what the tui poller listens on) or
    ``HERMES_SESSION_ID``. Two ids of one compacted lineage (same compression tip) are the same chat. A plain shell
    (neither set) and a Kanban worker are never warned."""
    title, tip, found = _title_and_tip(chat_id)
    label = title or "untitled"
    print(f"  chat {chat_id}: {label}" if found else
          f"  chat {chat_id} is not a chat in this profile's state.db (check the id)")
    mine = (os.environ.get("HERMES_SESSION_KEY") or os.environ.get("HERMES_SESSION_ID") or "").strip()
    if os.environ.get("HERMES_KANBAN_TASK") or not mine or mine == chat_id:
        return
    _mine_title, mine_tip, mine_found = _title_and_tip(mine)
    if found and mine_found and tip == mine_tip:
        return
    print(f"WARNING: binding a different chat ({label}): this command runs from chat {mine}, but --chat-id is "
          f"{chat_id}. To link the card to this chat, re-run with --chat-id {mine} and "
          f"`hermes kanban notify-unsubscribe {task_id} --platform tui --chat-id {chat_id}`.", file=sys.stderr)
