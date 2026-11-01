"""Recover cross-gateway dependencies skipped by an older live-feed partition."""

from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_db_connect import write_txn


def repair_partition_links(live: sqlite3.Connection, replica: sqlite3.Connection, gateway: str) -> list[str]:
    """Restore replica edges from foreign parents to cards in this file, without echoing them.

    The old partition consumed these changes without applying them, and a newer filter cannot recover them by
    pulling from the advanced cursor. Two later paths lost them the same way: evicting a picked-up foreign parent
    dropped its edges to our children, and a picked-up copy arrived without its own parent edges (hub 2026-10-29,
    t_01a189f115bdb5ef promoted 51 times against the Board's parents_not_done). So every edge whose child is in
    this file is restored, whether the parent is a local copy or absent. Only primary boards may replay them;
    local tombstones and unanswered operations retain precedence.
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
        present = {r[0] for r in live.execute("SELECT id FROM tasks")}
        # Only edges that will be inserted toggle ``applying``: an edge to a card not in this file stays a
        # candidate every round, and an unconditional toggle rewrote the sync state (a WAL commit) each round for
        # nothing. [daveey]
        todo = [(parent, child) for parent, child in edges if child in present and not any(
            key in held for key in (("task_links", f"{parent}>{child}"), ("tasks", parent), ("tasks", child)))]
        if not todo:
            return []
        applying = sync.sync_state(live)["applying"]
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        for parent, child in todo:
            uid = f"{parent}>{child}"
            if live.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                            (parent, child)).rowcount:
                restored.append(uid)
        live.execute("UPDATE kanban_sync_state SET applying = ?", (applying,))
    if restored:
        sync.wake_dispatcher()
    return restored
