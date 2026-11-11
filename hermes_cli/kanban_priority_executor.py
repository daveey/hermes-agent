"""Origin-scoped priority operation for the existing non-worker supervisor executor."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermes_cli import kanban_db as kb


def apply_attached(conn, attachment_id: int, digest: str, *, writer: str) -> dict:
    """Apply only this origin's rows from an already attached global manifest."""
    from hermes_cli.kanban_db_sync import sync_state
    from hermes_cli.kanban_supervisor_requests import _non_worker_refusal

    refusal = _non_worker_refusal()
    if refusal:
        raise PermissionError(refusal)
    changes = []
    with kb.write_txn(conn):
        identity = sync_state(conn) or {}
        if identity.get("mode") != "primary":
            raise ValueError("normalization requires a primary writer")
        if identity.get("gateway_id") != writer:
            raise ValueError("different writer; explicitly name this origin")
        attachment = kb.get_attachment(conn, attachment_id)
        if attachment is None:
            raise ValueError("attached manifest required")
        data = Path(attachment.stored_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("manifest digest mismatch")
        manifest = json.loads(data)
        if manifest["board_id"] != identity.get("board_id"):
            raise ValueError("manifest belongs to a different board")
        rows = [r for r in manifest["rows"] if r["origin"] == writer]
        live = {r["id"]: r["priority"] for r in conn.execute(
            "SELECT id,priority FROM tasks WHERE created_on=? AND status NOT IN ('done','archived')", (writer,))}
        if set(live) != {r["id"] for r in rows}:
            raise ValueError("ownership or membership changed; capture a new manifest")
        if all(live[r["id"]] == r["new"] for r in rows):
            return {"changed": 0, "writer": writer, "manifest_sha256": digest}
        if any(live[r["id"]] != r["old"] for r in rows):
            raise ValueError("priorities changed; capture a new manifest")
        for row in rows:
            current = live[row["id"]]
            if current == row["new"]:
                continue
            conn.execute("UPDATE tasks SET priority=? WHERE id=?", (row["new"], row["id"]))
            kb._append_event(conn, row["id"], "reprioritized", {
                "priority": row["new"], "previous_priority": current,
                "reason": "priority normalization v2", "manifest_sha256": digest,
            })
            changes.append(row["id"])
    for task_id in changes:
        kb.notify_task_updated(conn, task_id, ["priority"])
    return {"changed": len(changes), "writer": writer, "manifest_sha256": digest}
