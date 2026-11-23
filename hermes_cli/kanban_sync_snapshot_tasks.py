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
        changes = [ksc._row_change("tasks", r) for r in page.get("rows", [])]
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


def _unmoved(conn: sqlite3.Connection, changes: list) -> list:
    """The changes whose card holds exactly that Board version here (a newer row waits for the feed)."""
    if not changes:
        return []
    ids = [ch.uid for ch in changes]
    held = dict(conn.execute(f"SELECT id, sync_version FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids))
    return [ch for ch in changes if ch.version and held.get(ch.uid) == ch.version]
