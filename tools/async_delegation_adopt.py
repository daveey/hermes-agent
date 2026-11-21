"""Re-register an already claimed local unit; never claim or retry abandoned work.

Receipt/ownership transfer belongs to the recovery caller. This scheduling seam preserves
its durable row, original identity and finished siblings through the normal finalizer.
"""
from __future__ import annotations

import json
import os
import sqlite3


def owned_row(delegation_id):
    from tools import async_delegation as ad
    from gateway.status import get_process_start_time, start_time_fingerprints_match

    ad.require_ledger_owner()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.row_factory = sqlite3.Row
        found = conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
    if found is None:
        return None
    row = dict(found)
    started = get_process_start_time(os.getpid())
    try:
        same_owner = bool(row["owner_started_at"] and started and
                          start_time_fingerprints_match(row["owner_started_at"], started))
    except (TypeError, ValueError):
        same_owner = False
    if (row["owner_pid"] != os.getpid() or not same_owner
            or row["state"] != "running" or row["delivery_state"] != "pending"
            or row["completed_at"] is not None or row["event_json"] is not None):
        return None
    return row


def restore_owned_record(record, expected):
    """Caller holds the registry lock: refuse duplicate registration and stale reads."""
    from tools import async_delegation as ad

    delegation_id = record["delegation_id"]
    if delegation_id in ad._records or owned_row(delegation_id) != expected:
        return False
    task = json.loads(expected["task_json"])
    partial = json.loads(expected["result_json"] or "{}")
    for key in ad._ROUTING_KEYS:
        record.pop(key, None)
        if key in task:
            record[key] = task[key]
    record["dispatched_at"] = expected["dispatched_at"]
    record["_finished_children"] = {
        entry["task_index"]: entry for entry in partial.get("results", [])
        if isinstance(entry.get("task_index"), int)
    } if partial.get("partial") else {}
    return True


def adopt_unit(delegation_id, *, runner, interrupt_fn=None, progress_fn=None, max_async_children=3):
    """Schedule an owner-claimed running row once, without replacing its durable ledger.

    No ownership CAS, auto-resume policy bypass or parked state is implemented here.
    A failed schedule retains the row so its caller can safely release its claim.
    """
    from tools import async_delegation as ad

    row = owned_row(delegation_id)
    if row is None:
        return {"status": "rejected", "error": "Delegation is not an unfinished unit owned by this process"}
    task = json.loads(row["task_json"])
    partial = json.loads(row["result_json"] or "{}")

    def run_remaining():
        result = runner() or {}
        if partial.get("partial") and result.get("results"):
            combined = {entry["task_index"]: entry for entry in partial["results"]}
            combined.update((entry["task_index"], entry) for entry in result["results"])
            indexes = task.get("task_indexes")
            if indexes is None:
                indexes = range(len(task.get("goals") or []))
            return {**result, "results": [combined[i] for i in indexes if i in combined]}
        return result

    return ad._dispatch(
        delegation_id=delegation_id, goal=task.get("goal", ""),
        goals=task.get("goals") if task.get("is_batch") else None,
        context=task.get("context"), toolsets=task.get("toolsets"), role=task.get("role", "leaf"),
        model=task.get("model"), session_key=row["origin_session"] or "",
        parent_session_id=row["parent_session_id"], origin_ui_session_id=row["origin_ui_session_id"] or "",
        origin_session_id=row["origin_session_id"] or "", runner=run_remaining,
        interrupt_fn=interrupt_fn, progress_fn=progress_fn, max_async_children=max_async_children,
        capacity_error="Async delegation capacity reached while adopting unit",
        slot_key=task.get("slot_key"), task_indexes=task.get("task_indexes"),
        task_transcripts=task.get("task_transcripts"), task_session_ids=task.get("task_session_ids"),
        _adopt_row=row,
    )
