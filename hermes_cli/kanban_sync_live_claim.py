"""A Board ``live_claim`` refusal of a run or lifecycle-event row: record it and restore the local row. [daveey]

The Board (hermes-hub #111, ``rows.ts`` ``rowFence``) refuses ``live_claim`` not only a non-holder's task lifecycle
columns but also its run handoff (``task_runs`` status/outcome/ended_at/summary/metadata, a synthesized run
included) and its lifecycle events (``completed``, ``blocked``, ...) while another gateway holds the card's live
lease. Before that fence the run and event reached the Board and the completion landed half applied
(t_01a1d5386cb55abf). Here the client keeps that refusal visible on the card (a ``sync_rejected`` event, as for the
task row) and, on a primary board, puts the local row back to what the Board kept, so this mirror never holds a
handoff the Board refused:

- the Board returned its row: apply it over the local one, even though this gateway already holds that version
  (``_local_row_is_newer`` would otherwise skip a copy at or below the run's ``sync_version``);
- the Board has no such row (a phantom synthesized run, a ``completed`` event): delete the local one, tombstoned,
  so ``reconcile`` never re-uploads it after the lease ends.

A shadow (non-primary) board leaves its live rows alone, as for the task row: the live file is still the local
authority there and the shadow copy follows the feed.
"""

from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db_sync as sync

# Tables whose live_claim refusal is recorded on the card. The task row was the only one before #111.
RECORDED_TABLES = frozenset({"tasks", "task_runs", "task_events"})
CHILD_TABLES = frozenset({"task_runs", "task_events"})


def is_live_claim(row: dict, res: dict) -> bool:
    return row["table_name"] in RECORDED_TABLES and res.get("reason") == "live_claim"


def refused(conn: sqlite3.Connection, row: dict, res: dict, reason: str, primary: bool,
            authoritative: list) -> bool:
    """Record a live_claim refusal on the card; for a child row on a primary board queue its restore into
    ``authoritative`` and return True (the caller then skips the generic returned-row apply)."""
    from hermes_cli.kanban_sync_compat import record_rejection
    record_rejection(conn, row, reason)
    if not primary or row["table_name"] not in CHILD_TABLES:
        return False
    authoritative.extend(restore_child(conn, row, res))
    return True


def restore_child(conn: sqlite3.Connection, row: dict, res: dict) -> list[sync.RemoteChange]:
    """The changes that make a refused child row match the Board (primary boards; the caller applies them in the
    push's authoritative batch). Prepares the local run so the Board's copy is not skipped as already seen."""
    table, uid = row["table_name"], str(row["row_uid"])
    # ``compat`` is ``enqueue_retry``'s reduced retry after a forbidden_field rejection, sent as an upsert.
    if table not in CHILD_TABLES or row["op"] not in ("upsert", "compat"):
        return []
    board_row = res.get("row")
    if board_row:
        if table == "task_runs":
            conn.execute("UPDATE task_runs SET sync_version = NULL WHERE uid = ?", (uid,))
        return [sync.RemoteChange(seq=0, table=table, uid=str(board_row["uid"]), op="upsert", fields=board_row,
                                  version=int(board_row.get("version") or 0), hlc=str(board_row.get("hlc") or ""),
                                  origin_gateway=str(board_row.get("origin_gateway") or ""))]
    return [sync.RemoteChange(seq=0, table=table, uid=uid, op="delete", fields={}, version=0, hlc="",
                              origin_gateway="")]
