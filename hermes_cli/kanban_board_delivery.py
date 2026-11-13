"""What a Kanban write's result says while this gateway's Board sync is stopped. [daveey]

Incident (2026-11-13, David-MBP): the gateway gave up the sync role (``SchemaAhead``) at 23:53 PT. For the next hour
``kanban_comment`` on a card whose live run was on worker-7 kept returning ``live_run: true`` and "the card has a
live worker run: it gets this note at its next model call". The comments (10761, 10772, 10776) sat in the local
``kanban_sync_outbox`` (pending, 0 attempts) and never reached the Board or the run; a chat told David a relayed
request was delivered. The delivery text read only the local row's live run, never whether this gateway can push.

:func:`queued_local` is the one check: on a board that syncs, while :mod:`kanban_sync_runstate` records the loop as
down (role given up, refused start, crash, stop), every write stays in this file until the loop recovers. Tool
results then carry ``board_delivery: "queued_local"`` and ``board_sync`` (the sentence below); a comment on a card
whose live run another gateway holds drops the live-run promise and names that gateway as the route.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

__all__ = ["QUEUED_LOCAL", "cli_lines", "queued_local", "remote_runner"]

QUEUED_LOCAL = "queued_local"


def _stopped(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """The loop's down record when ``conn``'s board syncs to the Board, else None (running, or an unsynced board,
    where the local file is the whole story)."""
    from hermes_cli import kanban_sync_runstate as runstate
    record = runstate.down()
    if not record:
        return None
    from hermes_cli import kanban_db_sync as sync
    try:
        if sync.board_mode(conn) == "off":
            return None
    except sqlite3.Error:
        return None
    return record


def remote_runner(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The gateway holding the card's live run when that is not this gateway (the Board lease mirror), else None."""
    try:
        row = conn.execute("SELECT status, current_run_id, lease_holder FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        own = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] != "running" or row[1] is None or not row[2]:
        return None
    holder = str(row[2])
    return None if own and own[0] == holder else holder


def queued_local(conn: sqlite3.Connection, task_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Result fields for a write that stays local because the sync is stopped, else None (sync runs or is off).

    ``task_id``: the comment's card; when another gateway runs it, ``live_run_host`` and ``route`` name the
    working path (the comment is not delivered to that run until the sync recovers)."""
    record = _stopped(conn)
    if not record:
        return None
    from hermes_cli import kanban_sync_runstate as runstate
    out: dict[str, Any] = {
        "board_delivery": QUEUED_LOCAL,
        "board_sync": (f"queued locally: this gateway's Board sync has been stopped since "
                       f"{runstate._when(record.get('since'))} ({record.get('reason') or 'unknown reason'}); "
                       "other hosts and remote runs won't see this until it recovers"),
    }
    runner = remote_runner(conn, task_id) if task_id else None
    if runner:
        out["live_run_host"] = runner
        out["route"] = (f"the card's live run is on {runner} and will not get this note: to reach it now, run "
                        f"`hermes kanban comment {task_id} \"...\"` on {runner}")
    return out


def cli_lines(conn: sqlite3.Connection, task_id: Optional[str] = None) -> list[str]:
    """The same notice as CLI output lines (empty while the sync runs)."""
    note = queued_local(conn, task_id)
    if not note:
        return []
    return [note["board_sync"][0].upper() + note["board_sync"][1:] + "."] + (
        [note["route"][0].upper() + note["route"][1:] + "."] if note.get("route") else [])
