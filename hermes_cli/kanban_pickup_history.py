"""Refill a picked-up card's truncated event history from the replica. [daveey]

Before the full-history pickup, ``kanban_worker_node._changes_for`` copied only a card's newest 200 ``task_events``.
A long card re-picked-up by a worker node then held its runs but not their early events, so the run's
``owner_admitted`` (the session that ran it) was gone and ``hermes-core-wt ship`` refused to land:
``run N started an agent but recorded no session`` (t_01a1ccf95e85a6f1, 2026-11-12).

:func:`refill` repairs such a copy from this board's replica (the Board's full mirror), never inventing a row:

* Foreign copies refill only their missing prefix. Rehomed own cards additionally recover published
  pre-handoff gaps through ``kanban_rehome_history``; present ownership is not proof of complete history.
* Only the missing *prefix* is filled: replica events older than the earliest event the copy already holds, matched
  by ``uid``. A newer gap is not a truncation and the feed delivers it; refilling it could race that feed.
* Every refilled row keeps its replica ``uid``, ``kind``, ``payload`` and ``created_at``; its run is the local run
  with the same run uid. An event whose run is not here is skipped (reported ``unresolved``), never re-homed.
* A refilled row takes a negative local id below every existing one, oldest lowest. It therefore sorts before the
  copy's live history, is never "the latest X" (``ORDER BY id DESC``), and never crosses a notifier cursor
  (``id > last_event_id``), so no subscriber is re-sent old events. Rows are inserted with capture suppressed, so
  nothing is pushed back to the Board.

It is idempotent and safe on a running card (one write transaction). The dispatcher's pickup runs it for every
local copy each tick; ``hermes kanban sync refill-history`` runs it on demand.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from hermes_cli import kanban_db_sync as sync


def foreign_copies(conn: sqlite3.Connection, own: str) -> list[str]:
    """Ids of the local copies of other gateways' cards that are still open (gc trims terminal cards' events)."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ? AND status NOT IN ('done', 'archived') "
        "ORDER BY id", (own,))]


def _missing_prefix(conn: sqlite3.Connection, replica: sqlite3.Connection, tid: str) -> Optional[list[sqlite3.Row]]:
    """Replica events older than the copy's earliest held one (oldest first); None when the copy holds none."""
    held = {r[0] for r in conn.execute("SELECT uid FROM task_events WHERE task_id = ? AND uid IS NOT NULL", (tid,))}
    if not held:
        return None
    out: list[sqlite3.Row] = []
    for row in replica.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (tid,)):
        if row["uid"] in held:
            return out
        if row["uid"] is not None:
            out.append(row)
    return None  # the replica has none of the copy's events: no anchor, nothing provably older


def refill(conn: sqlite3.Connection, replica: sqlite3.Connection, own: str,
           ids: Optional[Iterable[str]] = None) -> dict[str, dict]:
    """Fill each copy's missing event prefix from the replica. ``{task_id: {"filled", "unresolved", "reason"?}}``;
    with ``ids`` None every foreign copy is checked and only cards with something to report are returned."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_db_connect import write_txn
    explicit = ids is not None
    targets = list(ids) if explicit else foreign_copies(conn, own)
    from hermes_cli.kanban_rehome_history import recover
    report: dict[str, dict] = recover(conn, replica, own, targets if explicit else None)
    for tid in targets:
        if tid in report:
            continue
        card = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (tid,)).fetchone()
        if card is None or card[0] in (None, own):
            if explicit:
                report[tid] = {"filled": 0, "unresolved": 0,
                               "reason": "not here" if card is None else "this gateway's own card (authoritative)"}
            continue
        missing = _missing_prefix(conn, replica, tid)
        if not missing:
            if explicit:
                report[tid] = {"filled": 0, "unresolved": 0,
                               **({"reason": "no anchor event in the replica"} if missing is None else {})}
            continue
        wire = [(row["uid"], ksc._row_fields(replica, "task_events", row)) for row in missing]
        filled = unresolved = 0
        with write_txn(conn):
            conn.execute("UPDATE kanban_sync_state SET applying = 1")
            try:
                held = {r[0] for r in conn.execute("SELECT uid FROM task_events WHERE task_id = ?", (tid,))}
                low = conn.execute("SELECT MIN(id) FROM task_events").fetchone()[0]
                next_id = min(0, low or 0) - 1
                for uid, fields in reversed(wire):  # newest first: the oldest gets the lowest id
                    if uid in held or sync._tombstoned(conn, "task_events", uid):
                        continue
                    run_uid = fields.get(sync._wire_name("task_events", "run_id"))
                    if run_uid is not None and not conn.execute(
                            "SELECT 1 FROM task_runs WHERE uid = ? AND task_id = ?", (run_uid, tid)).fetchone():
                        unresolved += 1  # its run is not here: never re-home it or mint a placeholder run
                        continue
                    cols = sync._local_fields(conn, "task_events", fields, tid)
                    cols.pop("id", None)
                    cols.pop("uid", None)
                    if cols.get("task_id", tid) != tid:
                        unresolved += 1
                        continue
                    cols["task_id"] = tid
                    names = ["id", "uid", *cols]
                    conn.execute(f"INSERT INTO task_events ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                                 (next_id, uid, *cols.values()))
                    next_id -= 1
                    filled += 1
            finally:
                conn.execute("UPDATE kanban_sync_state SET applying = 0")
        report[tid] = {"filled": filled, "unresolved": unresolved}
    return report
