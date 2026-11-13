"""Released chat links to closed cards, for readers that list closed cards too. [daveey]

``replica_cards`` lists a chat's unreleased links: open work, and closed cards whose release is not yet verified
(``unknown``). A link is released once the card closed with a verified release, so a reader that asks for closed
cards (``hermes kanban chat-tasks``, statuses including ``done``/``archived``) lost the card entirely the moment it
was released: the chat's done hub cards were absent rather than ``done`` (hub t_01a1d4f2f591d595 et al.,
2026-11-13). This reads those links back as explicit released rows, with the reason, for those readers only; the
sidebar counts never ask for closed cards and are unchanged.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

TERMINAL_STATUSES = ("done", "archived")
REASON = "link_released"  # the chat's link was released: the card closed with a verified release


def read(path: Path, gateway: str, profile: str, sessions: Iterable[str],
         statuses: Iterable[str]) -> list[dict[str, Any]]:
    """Released links of these chats (this gateway+profile) to cards another gateway created, closed in one of
    ``statuses``. Raises ``sqlite3.Error`` when unreadable."""
    from hermes_cli.kanban_session_activity import cached_ro  # lazy: that module imports kanban_chat_links
    wanted = [s for s in TERMINAL_STATUSES if s in set(statuses)]
    chats = sorted({str(s) for s in sessions if s})
    if not wanted or not chats:
        return []
    with cached_ro(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT l.task_id AS id, t.title, t.status, t.assignee, t.created_at, t.started_at, t.priority, "
            "t.created_on, l.chat_session, l.chat_profile, l.kind "
            "FROM task_chat_links l JOIN tasks t ON t.id = l.task_id "
            "WHERE l.chat_gateway = ? AND l.chat_profile = ? AND l.released = 1 "
            f"AND l.chat_session IN ({','.join('?' * len(chats))}) "
            f"AND t.status IN ({','.join('?' * len(wanted))}) "
            "AND t.created_on IS NOT NULL AND t.created_on != ? ORDER BY l.kind",
            (gateway, profile, *chats, *wanted, gateway)).fetchall()]


def row(r: dict[str, Any], *, board: str, gateway: str, key: str, owner: str | None, owner_profile: str,
        pulled: float | None, stale: bool) -> dict[str, Any]:
    """The listed shape of one released link (``replica_cards``' row, closed and released)."""
    return {
        "title": r["title"], "status": r["status"], "assignee": r["assignee"], "created_at": r["created_at"],
        "started_at": r["started_at"], "priority": r["priority"], "block_kind": None, "human_request": None,
        "id": str(r["id"]), "board": board, "gateway": owner, "key": key, "session": r["chat_session"],
        "session_id": r["chat_session"], "via": "origin" if r["kind"] == "created" else "shepherd", "remote": True,
        "profile": owner_profile, "owner_profile": owner_profile, "origin_gateway": gateway,
        "origin_profile": r["chat_profile"], "ownership_state": "qualified" if owner is not None else "unknown",
        "unfinished": False, "released": True, "release_reason": REASON, "reported_status": r["status"],
        "synced_at": pulled, "stale": stale, "source": "replica",
    }
