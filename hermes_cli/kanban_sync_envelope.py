"""Durable first-send envelopes, committed by push before network I/O. [daveey]

Only pending transport is retained: acknowledged outbox rows are deleted normally;
synthetic parents are removed on acknowledgement or when their task disappears.
This cannot reconstruct envelopes already transmitted by an older core.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable


def outbound(conn: sqlite3.Connection, row: dict, build: Callable[[], dict]) -> dict:
    """Rebuild neither a transmitted payload nor its ordering clock on uncertain retry."""
    saved = conn.execute("SELECT wire_json FROM kanban_sync_outbox WHERE op_id=?", (row["op_id"],)).fetchone()
    if saved and saved[0] is not None:
        wire = json.loads(saved[0])
        if wire["client_op_id"] == row["op_id"]:
            return wire
        # Explicit lifecycle/schema repair can replace an op's identity in place.
        # Its former envelope belongs to the retired id, never the replacement.
    wire = build()
    conn.execute("UPDATE kanban_sync_outbox SET wire_json=?, hlc=? WHERE op_id=?",
                 (json.dumps(wire, separators=(",", ":")), wire.get("hlc", ""), row["op_id"]))
    return wire


def parent(conn: sqlite3.Connection, task_id: str, build: Callable[[], dict]) -> dict:
    """A synthesized parent is pending work too; a lost answer must not mint a new op."""
    saved = conn.execute("SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?", (task_id,)).fetchone()
    if saved:
        return json.loads(saved[0])
    wire = build()
    conn.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES (?,?)",
                 (task_id, json.dumps(wire, separators=(",", ":"))))
    return wire


def retire_parent(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM kanban_sync_parent_envelopes WHERE task_id=?", (task_id,))


def prune_parents(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM kanban_sync_parent_envelopes WHERE NOT EXISTS "
                 "(SELECT 1 FROM tasks WHERE id=task_id AND sync_version IS NULL)")
