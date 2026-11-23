"""After a snapshot rebuild, write each card's snapshot row again over what the history replay made of it.

:func:`kanban_sync_client.resync` imports tables in ``IMPORT_ORDER``: ``tasks`` first, then the children. A
``task_events`` page replays a card's whole containment history, and the containment projection
(``kanban_containment_replica.restore_page``) rewrites the card's status from it: a historical ``contained`` holds
the card (``triage``, ``completed_at`` cleared) and its later ``containment_released`` lands it on its parents'
status (``ready``). In the live feed a newer task write follows those events and wins. In a snapshot nothing
follows them, so the card keeps the replayed status under the Board's version of the finished card.

Fleet incident 2026-11-23: every worker that built its replica after a contained card had finished (worker-a3,
worker-a5) held six done/archived cards as ``ready`` at the Board's version. The Board refused their leases as
``not_claimable`` and worker-a3 logged "dispatcher stuck" every few minutes; no repair reached them, since
divergence repair only compares a gateway's own cards. Reproduced on the hub by rebuilding a scratch replica: the
cards read ``done`` after the ``tasks`` pass and ``ready`` after ``task_events``.

So the rebuild reads the ``tasks`` snapshot again and re-applies each row whose version is still the one the first
pass wrote. A row the Board moved past in between is left for the feed, which resumes at the first pass's head and
delivers that change in order. [daveey]
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db_sync as sync


def finish_rebuild(conn: sqlite3.Connection, hub: Any, board: str, head: int, *,
                   partition: Optional[str] = None) -> None:
    """The end of :func:`kanban_sync_client.resync`: re-assert the task rows, then resume the feed at ``head``."""
    from hermes_cli.kanban_db_connect import write_txn
    reassert(conn, hub, board, partition=partition)
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET cursor = ?", (head,))


def reassert(conn: sqlite3.Connection, hub: Any, board: str, *, partition: Optional[str] = None) -> int:
    """Re-apply the ``tasks`` snapshot rows still at the version the rebuild wrote; returns how many."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_sync_quarantine import certified_page
    from hermes_cli.kanban_sync_stop import checkpoint
    count, after = 0, ""
    while True:
        checkpoint()
        page = hub.snapshot_page(board, "tasks", after)
        changes = [_change(r) for r in page.get("rows", [])]
        if partition is not None:
            changes = ksc.own_partition(conn, changes, partition)
        changes = _unmoved(conn, changes)
        if changes:
            cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
            changes = certified_page(conn, hub, board, changes)
            sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board, observers=False,
                                      local_pending_wins=True)
            count += len(changes)
        if not page.get("more"):
            return count
        after = str(page["next"])


def _change(row: dict) -> sync.RemoteChange:
    """A snapshot row as the upsert the first pass applied."""
    return sync.RemoteChange(seq=0, table="tasks", uid=str(row["uid"]), op="upsert", fields=row,
                             version=int(row.get("version") or 0), hlc=str(row.get("hlc") or ""),
                             origin_gateway=str(row.get("origin_gateway") or ""))


_CHUNK = 500  # bound values per IN (...) read, as elsewhere in kanban sync


def _unmoved(conn: sqlite3.Connection, changes: list) -> list:
    """The changes whose card holds exactly that Board version here and differs from it in a synced column.

    A newer row waits for the feed; an identical one needs no write (most of a 22k-card board). A reference
    column (a local run id against the Board's run uid) is not compared."""
    refs = {column for table, column in sync.REF_COLUMNS if table == "tasks"}
    compared = sync.SYNCED_COLUMNS["tasks"] - refs
    out = []
    for index in range(0, len(changes), _CHUNK):
        part = changes[index:index + _CHUNK]
        ids = [ch.uid for ch in part]
        local = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        for ch in part:
            row = local.get(ch.uid)
            if row is None or not ch.version or row["sync_version"] != ch.version:
                continue
            keys = compared & set(ch.fields) & set(row.keys())
            if any(row[k] != ch.fields[k] for k in keys):
                out.append(ch)
    return out
