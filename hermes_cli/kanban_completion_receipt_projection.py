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
        metadata = json.loads(row["receipt_metadata"] or "{}")
        if not isinstance(metadata, dict) or "completion_receipt" not in metadata:
            return None
        receipt = validate_completion_receipt(metadata["completion_receipt"])
    except (ValueError, TypeError):
        return None
    event_uid = row["uid"] if "uid" in row.keys() else None
    return {"completion_receipt": receipt, "completion_receipt_source": {
        "kind": "task_run", "ref": f"@task:{board}@{gateway}/{task_id}",
        "event_id": row["id"], "event_uid": event_uid,
        "run_id": row["run_id"], "run_uid": row["receipt_run_uid"],
        "completed_at": row["created_at"],
    }}
