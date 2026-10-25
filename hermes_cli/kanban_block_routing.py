"""Block transition policy, separate from the database facade."""
from __future__ import annotations

import sqlite3


def supervisor_stop(conn, task_id: str, reason: str, reason_code: str) -> bool:
    """Atomically park an automatic stop for agent investigation, never a human ask."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        changed = conn.execute(
            "UPDATE tasks SET status = 'triage', block_kind = 'transient' "
            "WHERE id = ? AND status IN ('ready', 'review')", (task_id,),
        ).rowcount
        if not changed:
            return False
        kb._append_event(conn, task_id, "supervisor_escalated", {
            "kind": "transient", "reason_code": reason_code, "reason": reason,
        })
    return True


def _normalized_reason(reason: str | None) -> str:
    return " ".join((reason or "").split()).casefold()


def route_block(
    kind: str | None, reason: str | None, source_status: str, *,
    prev_kind: str | None, prev_recurrences: int, prev_reason: str | None = None,
    compare_reason: bool = False,
) -> tuple[str, str, str, tuple, dict]:
    """Return status, event, SQL fields/params and payload for a block.

    Dependency waits never enter the human bucket. Repeated blocks retain the
    loop breaker; a changed human question resets it because an answer is progress.
    """
    from hermes_cli import kanban_db as kb

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
    if recurrences >= kb.BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = kb.BLOCK_RECURRENCE_LIMIT
        if kind == "needs_input" and not compare_reason:
            # A dependency manufactured this kind; no human question was recorded.
            payload.update(requested_kind=kind, rekind_reason="agent_block_loop", kind="transient")
            kind = "transient"
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def block_if_guard_hold_capped(conn, task_id: str, reason: str) -> bool:
    """Block ``task_id`` when the guard has held it for ``reason`` past the cap. [daveey]"""
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch_quota as _kbq
    import time

    cap = dispatch.guard_hold_cap_seconds(reason)
    if cap is None:
        return False
    started = dispatch._guard_hold_started_at(conn, task_id)
    if started is None:
        return False
    held = int(time.time()) - started
    if held < cap:
        return False
    row = conn.execute("SELECT last_failure_error FROM tasks WHERE id = ?", (task_id,)).fetchone()
    raw_err = _kb._lossy_text(row["last_failure_error"]) if row else ""
    if reason == "rate_limit_cooldown" or _kbq.is_quota_error(raw_err):
        # A quota wall clears on its own: never a human block. Drop the stamp (the event it
        # appends restarts the hold clock) and let the next tick retry. [daveey]
        if raw_err:
            _kbq.clear_quota_error(conn, task_id, raw_err)
        return False
    err = _kb._first_line(raw_err, 300) or "(no error recorded)"
    minutes = max(1, held // 60)
    message = (f"Held in ready by the respawn guard ({reason}) for {minutes} min without running. "
               f"Last error: {err} Fix the cause (credentials, PR binding), then unblock.")
    try:
        return supervisor_stop(conn, task_id, message, "respawn_guard_hold")
    except sqlite3.Error:
        _kb._log.exception("kanban: auto-blocking guard-held %s failed", task_id)
        return False
