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


def repair_partition_chat_links(live: sqlite3.Connection, replica: sqlite3.Connection, gateway: str) -> list[str]:
    """Recover this gateway's chats on picked-up foreign cards, never local edits or deletions.

    Eviction removes the copy's links without tombstones; pickup omits them and the feed cursor has
    already consumed their history. Restore only absent rows from the freshly pulled replica, without
    echoing an upload or changing the card/claim. Other gateways' chats remain replica-only.
    """
    from hermes_cli.kanban_placement import is_placeholder_session

    if sync.board_mode(live) != "primary":
        return []
    # Read rows, deletion guards and their cursor in ONE SQLite snapshot. The live pull may
    # already have consumed a newer delete for an absent row, without leaving a tombstone.
    candidates = replica.execute(
        "SELECT l.*, s.cursor AS replica_cursor FROM task_chat_links l CROSS JOIN kanban_sync_state s "
        "WHERE l.chat_gateway = ? AND l.uid IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM kanban_sync_tombstones d WHERE "
        "(d.table_name = 'task_chat_links' AND d.row_uid = l.uid) OR "
        "(d.table_name = 'tasks' AND d.row_uid = l.task_id))", (gateway,)).fetchall()
    if not candidates:
        return []
    restored = []
    with write_txn(live):
        state = sync.sync_state(live) or {}
        if int(candidates[0]["replica_cursor"] or 0) < int(state.get("cursor") or 0):
            return []
        foreign = {r[0] for r in live.execute(
            "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ?", (gateway,))}
        existing = {r[0] for r in live.execute("SELECT uid FROM task_chat_links")}
        held = {(t, u) for t, u in live.execute(
            "SELECT table_name, row_uid FROM kanban_sync_outbox WHERE state IN ('pending', 'rejected') "
            "AND table_name IN ('tasks', 'task_chat_links') UNION "
            "SELECT table_name, row_uid FROM kanban_sync_tombstones "
            "WHERE table_name IN ('tasks', 'task_chat_links')")}
        todo = [row for row in candidates
                if row["task_id"] in foreign and row["uid"] not in existing
                and not is_placeholder_session(row["chat_session"])
                and ("tasks", row["task_id"]) not in held
                and ("task_chat_links", row["uid"]) not in held]
        if todo:
            applying = (sync.sync_state(live) or {})["applying"]
            live.execute("UPDATE kanban_sync_state SET applying = 1")
            for row in todo:
                live.execute(
                    "INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, "
                    "kind, released, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(row[c] for c in ("uid", "task_id", "chat_gateway", "chat_profile", "chat_session",
                                          "kind", "released", "created_at")))
                restored.append(row["uid"])
            live.execute("UPDATE kanban_sync_state SET applying = ?", (applying,))
    return restored
