"""Optional upstream presentation for Board replicas, without a derived synced column. [daveey]"""
from __future__ import annotations

import sqlite3

from hermes_cli.kanban_session_activity_card_details import _add_blocked_upstream


def optional_select(conn: sqlite3.Connection) -> str:
    """Some inventory schemas carry a stamp; older ones have only the synced graph."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    return ", t.blocked_upstream" if "blocked_upstream" in columns else ""


def enrich(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    from hermes_cli.kanban_chat_links import _decoded_request

    # Card ids can occur once per linked chat. Compute once, then fan out to all links.
    cards = {r["id"]: {"status": r["status"]} for r in rows}
    # This connection is already the selected replica snapshot. A process-home
    # shadow cannot supply evidence for its graph (and may belong to another home).
    _add_blocked_upstream(conn, cards, replica=None)
    for row in rows:
        stamp = _decoded_request(row.get("blocked_upstream"))
        row.pop("blocked_upstream", None)
        # Match owner publication eligibility even for optional stored stamps.
        ancestor = None
        if row["status"] not in {"done", "archived", "blocked", "triage"}:
            ancestor = stamp or cards[row["id"]].get("blocked_upstream")
        if ancestor:
            row["blocked_upstream"] = ancestor
    from hermes_cli.kanban_producer_wait import stamp
    stamp(conn, rows)
    return rows


def fields(row: dict) -> dict:
    return {"blocked_upstream": row["blocked_upstream"]} if row.get("blocked_upstream") else {}
