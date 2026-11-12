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
        from hermes_cli.kanban_priority import PRIORITY_CAP, at_value
        manifest = validate(json.loads(data))
        if manifest["board_id"] != identity.get("board_id"):
            raise ValueError("manifest belongs to a different board")
        unknown = conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived') "
                               "AND (created_on IS NULL OR created_on='') LIMIT 1").fetchone()
        if unknown is not None:
            raise ValueError("missing origin in live inventory")
        from hermes_cli.kanban_priority_inventory import classify
        observed = [dict(r) for r in conn.execute("SELECT id,priority,created_on,status FROM tasks")]
        rows, report = classify(manifest['rows'], observed, writer=writer)
        live = {r['id']: r['priority'] for r in observed}
        # A row holds its manifest value, or for an above-cap original also the
        # cap: the sync pull bounds every received priority in place (no event,
        # no outbox op) while the Board keeps the raw value the manifest captured.
        if undo:
            if all(at_value(live[r["id"]], r["old"]) for r in rows):
                return {"changed": 0, "writer": writer, "manifest_sha256": digest, **report}
            if any(live[r["id"]] != r["new"] for r in rows):
                raise ValueError("priorities changed; capture a new manifest")
        else:
            if all(live[r["id"]] == r["new"] for r in rows):
                return {"changed": 0, "writer": writer, "manifest_sha256": digest, **report}
            if any(not at_value(live[r["id"]], r["old"]) for r in rows):
                raise ValueError("priorities changed; capture a new manifest")
        for row in rows:
            current = live[row["id"]]
            # Only the explicit legacy policy rollback restores above the cap.
            if not undo:
                value = row["new"]
            elif allow_legacy_rollback:
                value = row["old"]
            else:
                value = min(PRIORITY_CAP, row["old"])
            if current == value:
                continue
            conn.execute("UPDATE tasks SET priority=? WHERE id=?", (value, row["id"]))
            payload = {
                "priority": value, "previous_priority": current,
                "reason": "priority normalization rollback" if undo else "priority normalization v2",
                "manifest_sha256": digest,
            }
            if value != (row["old"] if undo else row["new"]):
                payload["manifest_priority"] = row["old"]  # capped legacy original, kept for audit
            kb._append_event(conn, row["id"], "reprioritized", payload)
            changes.append(row["id"])
    for task_id in changes:
        kb.notify_task_updated(conn, task_id, ["priority"])
    return {"changed": len(changes), "writer": writer, "manifest_sha256": digest, **report}
