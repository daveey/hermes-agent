"""Origin-scoped priority operation for the existing non-worker supervisor executor."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermes_cli import kanban_db as kb


def apply_attached(conn, attachment_id: int, digest: str, *, writer: str,
                   undo: bool = False, allow_legacy_rollback: bool = False) -> dict:
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
        owner = conn.execute("SELECT created_on FROM tasks WHERE id=?", (attachment.task_id,)).fetchone()
        if owner is None or owner[0] != writer:
            raise ValueError("attachment owner must belong to this writer")
        data = Path(attachment.stored_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("manifest digest mismatch")
        from hermes_cli.kanban_priority_manifest import validate
        from hermes_cli.kanban_priority import PRIORITY_CAP
        manifest = validate(json.loads(data))
        if manifest["board_id"] != identity.get("board_id"):
            raise ValueError("manifest belongs to a different board")
        rows = [r for r in manifest["rows"] if r["origin"] == writer]
        if undo and any(r["old"] > PRIORITY_CAP for r in rows) and not allow_legacy_rollback:
            raise ValueError("restoring above-cap values requires explicit legacy policy rollback")
        source, target = ("new", "old") if undo else ("old", "new")
        unknown = conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived') "
                               "AND (created_on IS NULL OR created_on='') LIMIT 1").fetchone()
        if unknown is not None:
            raise ValueError("missing origin in live inventory")
        live = {r["id"]: r["priority"] for r in conn.execute(
            "SELECT id,priority FROM tasks WHERE created_on=? AND status NOT IN ('done','archived')", (writer,))}
        if set(live) != {r["id"] for r in rows}:
            raise ValueError("ownership or membership changed; capture a new manifest")
        if all(live[r["id"]] == r[target] for r in rows):
            return {"changed": 0, "writer": writer, "manifest_sha256": digest}
        if any(live[r["id"]] != r[source] for r in rows):
            raise ValueError("priorities changed; capture a new manifest")
        for row in rows:
            current = live[row["id"]]
            if current == row[target]:
                continue
            conn.execute("UPDATE tasks SET priority=? WHERE id=?", (row[target], row["id"]))
            kb._append_event(conn, row["id"], "reprioritized", {
                "priority": row[target], "previous_priority": current,
                "reason": "priority normalization rollback" if undo else "priority normalization v2",
                "manifest_sha256": digest,
            })
            changes.append(row["id"])
    for task_id in changes:
        kb.notify_task_updated(conn, task_id, ["priority"])
    return {"changed": len(changes), "writer": writer, "manifest_sha256": digest}
