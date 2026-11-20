"""Pending local edits win over remote fields until the Board answers. [daveey]"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_db_sync as sync


def pending_task_fields(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Wire fields per task with a local edit still in the outbox (not rejected)."""
    out: dict[str, set[str]] = {}
    for uid, fields in conn.execute("SELECT row_uid, fields FROM kanban_sync_outbox "
                                    "WHERE table_name = 'tasks' AND op IN ('upsert', 'import') AND state != 'rejected'"):
        out.setdefault(uid, set()).update(json.loads(fields))
    return out


def without_pending(changes: list[sync.RemoteChange], pending: dict[str, set[str]]) -> list[sync.RemoteChange]:
    """A field with an unanswered local edit keeps the local value until that edit's answer arrives
    (RFC "Offline queue and reconnect": pull first, local-pending fields win locally)."""
    out = []
    for ch in changes:
        keep = pending.get(ch.uid) if ch.table == "tasks" and ch.op == "upsert" else None
        if keep:
            ch = sync.RemoteChange(ch.seq, ch.table, ch.uid, ch.op, {k: v for k, v in ch.fields.items() if k not in keep},
                                   ch.version, ch.hlc, ch.origin_gateway, ch.client_op_id)
        out.append(ch)
    return out
