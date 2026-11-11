"""Read the canonical completed-run receipt without re-admitting historical facts.

A newer review/blocked run is not a completion. A legacy latest completion has
unspecified artifact acceptance, even if an older run carried a receipt.
"""
from __future__ import annotations

import json

from hermes_completion_receipt import validate_completion_receipt


def read_completed(conn, task_id, *, gateway, board):
    """Return receipt plus source-qualified provenance, or None (legacy/invalid).

    Caller supplies the authoritative source namespace, not the observing host.
    Source event/run UIDs survive replica integer remapping; integer fallbacks
    are explicitly store-local. This is a projection, never a second writer.
    """
    if not gateway or not board:
        raise ValueError("completion_receipt: projection requires the source gateway and board")
    run_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    run_uid = "r.uid" if "uid" in run_columns else "NULL"
    row = conn.execute(
        f"SELECT e.*, r.metadata AS receipt_metadata, r.outcome AS receipt_outcome, "
        f"{run_uid} AS receipt_run_uid FROM task_events e "
        "LEFT JOIN task_runs r ON r.id=e.run_id AND r.task_id=e.task_id "
        "WHERE e.task_id=? AND e.kind='completed' ORDER BY e.created_at DESC,e.id DESC LIMIT 1",
        (task_id,)).fetchone()
    if row is None or row["receipt_outcome"] != "completed":
        return None
    try:
        event = json.loads((row["payload"] if "payload" in row.keys() else None) or "{}")
        if not isinstance(event, dict) or type(event.get("completion_receipt_admission")) is not int or event["completion_receipt_admission"] != 1:
            return None  # older writers may have stored arbitrary unadmitted metadata
        metadata = json.loads(row["receipt_metadata"] or "{}")
        if not isinstance(metadata, dict) or "completion_receipt" not in metadata:
            return None
        receipt = validate_completion_receipt(metadata["completion_receipt"])
    except (ValueError, TypeError):
        return None
    event_uid = row["uid"] if "uid" in row.keys() else None
    source = {
        "kind": "task_run", "ref": f"@task:{board}@{gateway}/{task_id}",
        "event_id": row["id"], "event_uid": event_uid,
        "run_id": row["run_id"], "run_uid": row["receipt_run_uid"],
        "completed_at": row["created_at"],
    }
    relocations = metadata.get("attached_from_workspace")
    if isinstance(relocations, dict):
        # Evidence keeps its observed identity. Carry the existing relocation
        # provenance alongside it, not as a new evidence observation.
        refs = {e["ref"] for item in receipt["items"] for e in item["evidence"]}
        refs.update(item["superseded_by"] for item in receipt["items"] if "superseded_by" in item)
        source["attached_from_workspace"] = {ref: dest for ref, dest in relocations.items()
                                             if ref in refs and isinstance(dest, str)}
    return {"completion_receipt": receipt, "completion_receipt_source": source}


def read_inventory(conn, task_id, *, board, gateway=None):
    """Optional projection for existing inventories, including older board schemas.

    A synced task's created_on is its authority even when another worker picked
    it up. Unsynced local boards use the configured gateway name. Unknown source
    or an old schema means no receipt, never fabricated acceptance.
    """
    import sqlite3
    from hermes_cli.kanban_origin import gateway_names

    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        source = row["created_on"] if "created_on" in row.keys() else None
        source = source or gateway or gateway_names()[0]
        if not source:
            return None
        return read_completed(conn, task_id, gateway=source, board=board)
    except sqlite3.Error:
        return None
