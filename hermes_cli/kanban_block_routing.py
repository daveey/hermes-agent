"""Typed block routing and recurrence accounting for the Kanban domain."""
from __future__ import annotations

import sqlite3
from typing import Optional


def latest_run_blocker(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Shared blocker of the latest non-neutral closed run."""
    from hermes_cli.kanban_db import _json_dict
    from hermes_cli.kanban_shared_blockers import NEUTRAL_OUTCOMES, normalize_blocker_id
    for row in conn.execute(
        "SELECT outcome, metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 20", (task_id,),
    ):
        bid = normalize_blocker_id(_json_dict(row["metadata"]).get("blocker_id"))
        if bid is not None:
            return bid
        if (row["outcome"] or "") not in NEUTRAL_OUTCOMES:
            return None
    return None


def latest_block_reason(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Reason of the newest blocked or block-loop event."""
    from hermes_cli.kanban_db import _json_dict
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'block_loop_detected') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    value = _json_dict(row["payload"]).get("reason")
    return value if isinstance(value, str) else None


def _normalized_reason(reason: Optional[str]) -> str:
    return " ".join((reason or "").split()).casefold()


def route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int, prev_reason: Optional[str] = None,
    compare_reason: bool = False,
) -> tuple[str, str, str, tuple, dict]:
    """Return status, event, SQL fields/params and audit payload for block_task.

    Dependency waits stay in todo. Other kinds count repeated blocks; changed
    human questions reset recurrence, but reworded impossible dependencies don't.
    """
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT
    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    same_cause = prev_kind == kind and (
        not (compare_reason and kind == "needs_input")
        or _normalized_reason(reason) == _normalized_reason(prev_reason)
    )
    recurrences = prev_recurrences + 1 if same_cause else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload
