"""Bounded, durable wire downgrade for an older Board's column allowlist."""
from __future__ import annotations

import json
import re
import sqlite3
import time

from hermes_cli import kanban_db_sync as sync

TRANSIENT_REASONS = ("storage_error", "depends_on_rejected")


def transient_rejection(res: dict) -> bool:
    reason = str(res.get("reason") or "")
    detail = str(res.get("detail") or "")
    return (reason in TRANSIENT_REASONS or (reason == "invalid" and "unknown table" in detail)
            or (reason == "not_found" and detail.startswith(("no task", "unknown task", "status origin pending queue full;"))))


def rejected_parent(conn: sqlite3.Connection, parents: list[tuple[dict, str]], result: dict) -> dict | None:
    """Keep successful parent versions; persist only schema-refused synthetic verdicts."""
    for op, tid in parents:
        if op["client_op_id"] != result.get("client_op_id"):
            continue
        if result.get("status") in ("applied", "merged") and result.get("version"):
            conn.execute("UPDATE tasks SET sync_version = MAX(COALESCE(sync_version, 0), ?) WHERE id = ?",
                         (result["version"], tid))
        from hermes_cli.kanban_sync_envelope import retire_parent
        if result.get("status") in ("applied", "merged"):
            retire_parent(conn, tid)
        if result.get("status") != "rejected":
            return None
        # Every explicit rejection is definitive, unlike a lost answer. Its cached
        # verdict must not poison the next synthesis after the refusal cause lifts.
        retire_parent(conn, tid)
        if result.get("reason") != "forbidden_field" and not transient_rejection(result):
            # Do not materialize permanent refusals as rejected rows/diagnostic
            # children: that grows one new row per pending-child round.
            return None
        # Only recoverable/schema refusals enter the existing bounded remap path.
        conn.execute(
            "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, hlc, created_at, status_origin) "
            "VALUES (?, 'tasks', ?, 'upsert', ?, ?, ?, ?)",
            (op["client_op_id"], tid, json.dumps(op["fields"]), op["hlc"], int(time.time() * 1000), op.get("status_origin")))
        return dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (op["client_op_id"],)).fetchone())
    return None


def record_rejection(conn: sqlite3.Connection, row: dict, reason: str) -> None:
    """Keep schema and lease refusals visible on the owning local card."""
    from hermes_cli import kanban_db as kb
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"] or "{}")
    tid = row["row_uid"] if row["table_name"] == "tasks" else fields.get("task_id")
    if tid is None and row["table_name"] in ("task_runs", "task_events"):  # a delta op names only what changed
        hit = conn.execute(f"SELECT task_id FROM {row['table_name']} WHERE uid = ?", (row["row_uid"],)).fetchone()
        tid = hit[0] if hit else None
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None:
        return
    # A diagnostic event must not recursively generate diagnostic events on an older Board.
    if row["table_name"] == "task_events" and fields.get("kind") == "sync_rejected":
        return
    payload = {"reason": reason[:300], "fields": sorted(fields), "op_id": row["op_id"]}
    if row["table_name"] != "tasks" and reason.startswith("live_claim"):  # refused handoff/event (hub #111) [daveey]
        payload.update(table=row["table_name"], uid=str(row["row_uid"]))
    kb._append_event(conn, tid, "sync_rejected", payload)


def enqueue_retry(conn: sqlite3.Connection, row: dict, wire: dict, result: dict) -> bool | str:
    """Retain the rejected original; one fresh, non-hydrated upsert carries the reduced payload.

    The local-only op kind survives crashes and transient retries. The durable omission ledger
    protects the local value while allowing unrelated fields to converge; a fresh authoritative
    field appearance can queue a current-value restore, never replay the rejected historical row.
    """
    from hermes_cli.kanban_sync_reserved_key import is_replacement
    if is_replacement(conn, row["op_id"]):
        return False  # keep the exact recovery refusal visible; never omit an approved recovery field
    if result.get("reason") in REASSESSMENT_REFUSALS:
        return downgrade_reassessment(conn, row, wire, result)
    if result.get("reason") != "forbidden_field" or row["op"] not in ("upsert", "import"):
        return False
    match = re.fullmatch(r"not writable on ([a-z0-9_]+): ([a-z0-9_]+(?:, [a-z0-9_]+)*)", str(result.get("detail") or ""))
    if match is None or match[1] != row["table_name"]:
        return False
    fields = wire.get("fields", {})
    named = match[2].split(", ")
    # A wire-only field (any Board too old to know it), or a source-fenced column (R4c: only a custody/manifest
    # card's Board refuses ``workspace_kind``/``branch_name``; every Board version accepts them otherwise), is
    # dropped with no omission, from a multi-field detail too (Q4); the Board's value comes back on the next pull.
    droppable = sync.VIRTUAL_WIRE_FIELDS.get(row["table_name"], frozenset()) | FENCED_FIELDS.get(row["table_name"], frozenset())
    if dropped := [f for f in named if f in droppable and f in fields]:
        return _drop_virtual(conn, row, wire, dropped)
    if len(named) != 1 or named[0] not in fields:
        return False
    field = named[0]
    reduced = {k: v for k, v in fields.items() if k != field}
    retry_id = sync.uuid7()
    inserted = conn.execute(
        "INSERT OR IGNORE INTO kanban_sync_omissions (original_op_id, table_name, row_uid, field, compat_op_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (row["op_id"], row["table_name"], row["row_uid"], field, retry_id)).rowcount
    if not inserted:
        return False
    conn.execute(
        "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at, status_origin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (retry_id, row["table_name"], row["row_uid"],
         "compat_import" if wire.get("import") else "compat", json.dumps(reduced),
         row.get("base_version"), wire["hlc"], row["created_at"], wire.get("status_origin")))
    return True


VIRTUAL_DROPPED = "virtual_dropped"
# An advertising Board's definitive refusal of the certificate itself (binding/time/transition). The park is
# still a valid ordinary park without it, so exactly these reasons downgrade to the saved ordinary origin.
REASSESSMENT_REFUSALS = ("owner_reassessment_binding", "owner_reassessment_time", "owner_reassessment_transition_lost")
_REASSESSMENT = ("owner_reassessment_v1", "owner_reassessment_fallback_status_origin")
# Real columns a new Board fences on custody/manifest cards (workspace_source v1, R4c): refused one per detail.
FENCED_FIELDS: dict[str, frozenset[str]] = {"tasks": frozenset({"workspace_kind", "branch_name"})}


def downgrade_reassessment(conn: sqlite3.Connection, row: dict, wire: dict, result: dict) -> bool | str:
    """Resend an opt-in park without its certificate after the Board definitively refused only that certificate.

    Only a known refusal of an op that actually carried the certificate and has its saved ordinary origin
    qualifies; anything else (unknown reasons, lost answers, other tables) is left untouched.
    """
    if (result.get("status") != "rejected" or result.get("reason") not in REASSESSMENT_REFUSALS
            or row["table_name"] != "tasks" or row["op"] != "upsert"
            or "owner_reassessment_v1" not in wire.get("fields", {})):
        return False
    stored = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"] or "{}")
    if not isinstance(stored.get("owner_reassessment_fallback_status_origin"), str):
        return False  # no saved ordinary origin: never invent one
    return _drop_virtual(conn, row, wire, ["owner_reassessment_v1"])


def _drop_virtual(conn: sqlite3.Connection, row: dict, wire: dict, dropped: list[str]) -> str:
    """The Board refused fields nothing local needs delivered: a wire-only field (``sync.VIRTUAL_WIRE_FIELDS``),
    or a source-fenced column (``FENCED_FIELDS``) whose Board value wins. Resend the same op without them under a
    fresh id, keeping the original op kind (a later refusal of another real column still takes the omission
    path), and retire the refused original: an omission row could never be delivered (``_queue_restore`` reads
    real columns only, and a fenced column stays fenced while custody exists) and a retained rejected op would hold
    the card from repair (``repair_holds``) forever."""
    stored = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
    origin = row.get("status_origin")
    if any(k in dropped for k in _REASSESSMENT):  # the two optional fields travel and leave together
        dropped = [*dropped, *(k for k in _REASSESSMENT if k not in dropped)]
        saved = stored.get("owner_reassessment_fallback_status_origin")
        if isinstance(saved, str):
            origin = saved  # the park uid is not the ordinary lifecycle origin an old Board validates
    reduced = {k: v for k, v in stored.items() if k not in dropped}
    if reduced or row["op"] != "upsert":  # an upsert of only refused fields has nothing left to say
        conn.execute(
            "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at, status_origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sync.uuid7(), row["table_name"], row["row_uid"], row["op"], json.dumps(reduced),
             row.get("base_version"), wire["hlc"], row["created_at"], origin))
    conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id = ?", (row["op_id"],))
    return VIRTUAL_DROPPED
