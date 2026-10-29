"""Recover cross-gateway dependencies skipped by an older live-feed partition."""

from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_db_connect import write_txn


def repair_partition_links(live: sqlite3.Connection, replica: sqlite3.Connection, gateway: str) -> list[str]:
    """Restore replica edges from foreign parents to local children, without echoing them.

    The old partition consumed these changes without applying them. A newer filter
    cannot recover them by pulling from the advanced cursor. Only primary boards
    may replay them; local tombstones and unanswered operations retain precedence.
    Run after both pulls and before refreshing the foreign-parent status mirror.
    """
    if sync.board_mode(live) != "primary":
        return []
    edges = list(replica.execute(
        "SELECT l.parent_id, l.child_id FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE p.created_on IS NOT NULL AND p.created_on != ?", (gateway,)))
    # A present replica link is authoritative even after a remote delete/re-add;
    # remote applies do not clear the replica's historical link tombstone.
    existing = {(p, c) for p, c in live.execute("SELECT parent_id, child_id FROM task_links")}
    edges = [(p, c) for p, c in edges if (p, c) not in existing]
    if not edges:
        return []
    restored = []
    with write_txn(live):
        held = {(t, u) for t, u in live.execute(
            "SELECT table_name, row_uid FROM kanban_sync_outbox WHERE state IN ('pending', 'rejected') "
            "UNION SELECT table_name, row_uid FROM kanban_sync_tombstones")}
        mine = {r[0] for r in live.execute(
            "SELECT id FROM tasks WHERE created_on = ? OR created_on IS NULL", (gateway,))}
        present = {r[0] for r in live.execute("SELECT id FROM tasks")}
        applying = sync.sync_state(live)["applying"]
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        for parent, child in edges:
            uid = f"{parent}>{child}"
            if child not in mine or parent in present:
                continue
            if any(key in held for key in (("task_links", uid), ("tasks", parent), ("tasks", child))):
                continue
            if live.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                            (parent, child)).rowcount:
                restored.append(uid)
        live.execute("UPDATE kanban_sync_state SET applying = ?", (applying,))
    if restored:
        sync.wake_dispatcher()
    return restored
