"""Remove obsolete lifecycle writes without replaying edited request bytes."""
from __future__ import annotations

import json
import sqlite3

LIFECYCLE = frozenset({
    "status", "current_run_uid", "completed_at", "started_at", "claim_lock", "claim_expires",
    "worker_pid", "worker_started_at", "human_request", "block_kind", "wake_at", "wake_check",
    "wake_check_every", "next_wake_check", "containment_episode", "lease_holder", "lease_expires",
})


def strip_lifecycle(conn: sqlite3.Connection, task: str) -> None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_outbox'").fetchone():
        return
    from hermes_cli.kanban_db_sync import uuid7
    rows = conn.execute("SELECT op_id,op,fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (task,)).fetchall()
    for op_id, op, raw in rows:
        fields = json.loads(raw)
        if not isinstance(fields, dict):
            raise ValueError("invalid containment outbox fields")
        kept = {key: value for key, value in fields.items() if key not in LIFECYCLE}
        if op != "upsert" or not kept:
            conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=?", (op_id,))
        elif kept != fields:
            # An old request may have reached the Board despite a lost reply.
            # Never reuse its replay identity with different serialized bytes.
            conn.execute("UPDATE kanban_sync_outbox SET op_id=?,fields=?,attempts=0,last_error=NULL WHERE op_id=?",
                         (uuid7(), json.dumps(kept, sort_keys=True, separators=(",", ":")), op_id))
