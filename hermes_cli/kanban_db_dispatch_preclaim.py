"""Pre-claim checks of one dispatch candidate (split out of ``kanban_db_dispatch``, file cap). [daveey]"""
from __future__ import annotations

import sqlite3


def note_nonspawnable(conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool) -> None:
    """Per-task diagnostic so ``show``/``tail`` name the missing profile instead of leaving the card in
    ``ready`` with zero board evidence (#122422). Unlike a respawn guard the condition never expires on
    its own, so write it once: a repeat only when something else happened on the card since (reassign,
    comment) — not one row per tick forever, and not one row per foreign home per tick on a shared
    board (#101015)."""
    if dry_run:
        return
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if (last is None or last["kind"] != "skipped_nonspawnable"
                or last["payload"] != kb._json_or_null({"assignee": assignee})):
            kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})


def eligible_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row], status: str,
                   result) -> list[sqlite3.Row]:
    from hermes_cli.kanban_worker_node import dispatch_refusals
    refused = dispatch_refusals(conn, status) if rows else {}
    if not refused:
        return rows
    kept = []
    for row in rows:
        reason = refused.get(row["id"])
        if reason is None:
            kept.append(row)
        else:
            result.skipped_ineligible.append((row["id"], reason))
    return kept
