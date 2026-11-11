"""Bounded, durable wire downgrade for an older Board's column allowlist."""
from __future__ import annotations

import json
import re
import sqlite3
import time

from hermes_cli import kanban_db_sync as sync


def rejected_parent(conn: sqlite3.Connection, parents: list[tuple[dict, str]], result: dict) -> dict | None:
    """Keep successful parent versions; persist only schema-refused synthetic verdicts."""
    for op, tid in parents:
        if op["client_op_id"] != result.get("client_op_id"):
            continue
        if result.get("status") in ("applied", "merged") and result.get("version"):
            conn.execute("UPDATE tasks SET sync_version = MAX(COALESCE(sync_version, 0), ?) WHERE id = ?",
                         (result["version"], tid))
        if result.get("status") != "rejected" or result.get("reason") != "forbidden_field":
            return None
        conn.execute(
            "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, hlc, created_at) "
            "VALUES (?, 'tasks', ?, 'upsert', ?, ?, ?)",
            (op["client_op_id"], tid, json.dumps(op["fields"]), op["hlc"], int(time.time() * 1000)))
        return dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (op["client_op_id"],)).fetchone())
    return None


def record_rejection(conn: sqlite3.Connection, row: dict, reason: str) -> None:
    """Keep schema and lease refusals visible on the owning local card."""
    from hermes_cli import kanban_db as kb
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"] or "{}")
    tid = row["row_uid"] if row["table_name"] == "tasks" else fields.get("task_id")
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None:
        return
    # A diagnostic event must not recursively generate diagnostic events on an older Board.
    if row["table_name"] == "task_events" and fields.get("kind") == "sync_rejected":
        return
    kb._append_event(conn, tid, "sync_rejected",
                     {"reason": reason[:300], "fields": sorted(fields), "op_id": row["op_id"]})


def enqueue_retry(conn: sqlite3.Connection, row: dict, wire: dict, result: dict) -> bool:
    """Retain the rejected original; one fresh, non-hydrated upsert carries the reduced payload.

    The local-only op kind survives crashes and transient retries. The durable omission ledger
    protects the local value while allowing unrelated fields to converge; a fresh authoritative
    field appearance can queue a current-value restore, never replay the rejected historical row.
    """
    if result.get("reason") != "forbidden_field" or row["op"] not in ("upsert", "import"):
        return False
    match = re.fullmatch(r"not writable on ([a-z_]+): ([a-z_]+)", str(result.get("detail") or ""))
    if match is None or match[1] != row["table_name"]:
        return False
    fields = wire.get("fields", {})
    if match[2] not in fields:
        return False
    reduced = {k: v for k, v in fields.items() if k != match[2]}
    retry_id = sync.uuid7()
    inserted = conn.execute(
        "INSERT OR IGNORE INTO kanban_sync_omissions (original_op_id, table_name, row_uid, field, compat_op_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (row["op_id"], row["table_name"], row["row_uid"], match[2], retry_id)).rowcount
    if not inserted:
        return False
    conn.execute(
        "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (retry_id, row["table_name"], row["row_uid"],
         "compat_import" if wire.get("import") else "compat", json.dumps(reduced),
         row.get("base_version"), wire["hlc"], row["created_at"]))
    return True
