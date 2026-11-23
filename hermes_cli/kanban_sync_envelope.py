"""Durable first-send envelopes, committed by push before network I/O. [daveey]

Only pending transport is retained: acknowledged outbox rows are deleted normally;
unissued synthetic parents may be removed when their task disappears. An issued
parent remains until exact final-verdict bookkeeping in the journal transaction.
This cannot reconstruct envelopes already transmitted by an older core.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable


REASSESSMENT_FIELDS = ("owner_reassessment_v1", "owner_reassessment_fallback_status_origin")


def gate_reassessment(row: dict, wire: dict, ok: bool) -> dict:
    """Serialization-time gate: the stored op keeps its captured binding; only the wire is downgraded.

    A Board that does not advertise ``owner_reassessment_v1`` gets neither optional field and the saved
    ordinary status origin, so the status still lands. Called before a wire is frozen.
    """
    fields = wire.get("fields")
    if ok or not isinstance(fields, dict) or not any(k in fields for k in REASSESSMENT_FIELDS):
        return wire
    saved = row.get("fields", {})
    saved = json.loads(saved) if isinstance(saved, str) else saved
    fallback = fields.get("owner_reassessment_fallback_status_origin",
                          saved.get("owner_reassessment_fallback_status_origin"))
    for key in REASSESSMENT_FIELDS:
        fields.pop(key, None)
    if "status_origin" in wire and isinstance(fallback, str):
        wire["status_origin"] = fallback
    return wire


def outbound(conn: sqlite3.Connection, row: dict, build: Callable[[], dict], *, supported: bool = True,
             reassessment: bool = False) -> dict:
    """Rebuild neither a transmitted payload nor its ordering clock on uncertain retry."""
    saved = conn.execute("SELECT wire_json FROM kanban_sync_outbox WHERE op_id=?", (row["op_id"],)).fetchone()
    if saved and saved[0] is not None:
        wire = json.loads(saved[0])
        if wire["client_op_id"] == row["op_id"]:
            if not supported:
                raise OSError("Board status-origin capability absent; immutable uncertain envelope held")
            if not reassessment and any(k in wire.get("fields", {}) for k in REASSESSMENT_FIELDS):
                raise OSError("Board reassessment capability absent; immutable uncertain envelope held")
            return wire
        # Explicit lifecycle/schema repair can replace an op's identity in place.
        # Its former envelope belongs to the retired id, never the replacement.
    wire = build()
    if not supported:
        return gate_reassessment(row, wire, reassessment)  # old Worker: retain legacy per-attempt rebuild/strip
    from hermes_cli.kanban_sync_status_origin import add_origin, freezeable
    wire = gate_reassessment(row, add_origin(conn, row, wire), reassessment)
    if not freezeable(row, wire):
        return wire  # historical unbound status is not upgraded by inventing an origin
    wire["frozen_replay"] = 1
    conn.execute("UPDATE kanban_sync_outbox SET wire_json=?, hlc=? WHERE op_id=?",
                 (json.dumps(wire, separators=(",", ":")), wire.get("hlc", ""), row["op_id"]))
    return wire


def parent(conn: sqlite3.Connection, task_id: str, build: Callable[[], dict], *, supported: bool = True) -> dict:
    """A synthesized parent is pending work too; a lost answer must not mint a new op."""
    saved = conn.execute("SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?", (task_id,)).fetchone()
    if saved:
        if not supported:
            raise OSError("Board status-origin capability absent; immutable uncertain parent held")
        return json.loads(saved[0])
    wire = build()
    if not supported:
        return wire
    try:
        captured = conn.execute("SELECT origin, status FROM kanban_sync_status_origins WHERE task_id=?", (task_id,)).fetchone()
    except sqlite3.Error:
        captured = None
    if "status" in wire.get("fields", {}):
        if not captured or captured[0] is None or captured[1] != wire["fields"]["status"]:
            return wire  # unknown historical intent: keep legacy rather than guessing
        wire["status_origin"] = captured[0]
    wire["frozen_replay"] = 1
    conn.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES (?,?)",
                 (task_id, json.dumps(wire, separators=(",", ":"))))
    return wire


def retire_parent(conn: sqlite3.Connection, task_id: str, *, remote: dict | None = None) -> None:
    from hermes_cli.kanban_replay_journal import parent_cleanup_hold
    parent_cleanup_hold(conn, task_id=task_id, remote=remote)
    conn.execute("DELETE FROM kanban_sync_parent_envelopes WHERE task_id=?", (task_id,))


def prune_parents(conn: sqlite3.Connection) -> None:
    from hermes_cli.kanban_replay_journal import parent_cleanup_hold
    held = parent_cleanup_hold(conn)
    conn.execute("DELETE FROM kanban_sync_parent_envelopes WHERE task_id IS NOT ? AND NOT EXISTS "
                 "(SELECT 1 FROM tasks WHERE id=task_id AND sync_version IS NULL)", (held,))
