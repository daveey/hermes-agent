"""Block transition policy, separate from the database facade."""
from __future__ import annotations

import sqlite3


def validate_block_request(human_request, kind):
    """Validate the human request before checking the block-kind vocabulary."""
    from hermes_cli.kanban_db import VALID_BLOCK_KINDS
    from hermes_cli.kanban_human_request import validate_human_request
    human_request = validate_human_request(human_request, kind=kind)
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    return human_request


def clear_meta(clear_blocker: bool) -> dict:
    """Run metadata for an explicit "the shared blocker is gone" park (``kanban_shared_blockers``)."""
    from hermes_cli.kanban_shared_blockers import CLEAR_FLAG
    return {CLEAR_FLAG: True} if clear_blocker else {}


def latest_run_blocker(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Shared blocker the card is still behind: the newest closed run that said anything about
    it (``kanban_shared_blockers.run_effect``; neutral outcomes and re-parks without the id pass)."""
    from hermes_cli.kanban_shared_blockers import run_effect
    for row in conn.execute(
        "SELECT outcome, metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 20", (task_id,),
    ):
        effect, bid = run_effect(row["outcome"], row["metadata"])
        if effect == "assert":
            return bid
        if effect == "clear":
            return None
    return None


def latest_block_reason(conn: sqlite3.Connection, task_id: str) -> str | None:
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


def has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """Explicit blocks and policy breakers need resolution, not a higher retry limit."""
    from hermes_cli.kanban_db import _json_dict
    # Task rows arrive before their event history during rehome/snapshot imports.
    # An outstanding ask is authoritative on the row; unblock_task clears it.
    task = conn.execute("SELECT human_request FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task and task["human_request"]:
        return True
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row and row["kind"] == "blocked":
        return True
    trip = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'gave_up' AND id > COALESCE("
        "  (SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'unblocked'), 0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id),
    ).fetchone()
    return bool(trip) and bool(_json_dict(trip["payload"]).get("sticky"))


def recovery_held(conn: sqlite3.Connection, row, failure_limit: int) -> bool:
    """True when ``recompute_ready`` must not auto-recover this ``blocked``/``triage`` row
    (``id``, ``status``, ``consecutive_failures``, ``max_retries``): a sticky stop that only
    ``unblock_task`` exits (#28712), or failures at the breaker limit (else block -> recover ->
    respawn -> exhaust -> block forever; the counter is kept so it accumulates across cycles).
    Limit order matches ``_record_task_failure``: ``max_retries`` > ``failure_limit``. The
    review-release precompute applies the same skips, so a held card costs no GitHub read."""
    if row["status"] not in ("blocked", "triage"):
        return False
    limit = row["max_retries"] if row["max_retries"] is not None else failure_limit
    return has_sticky_block(conn, row["id"]) or int(row["consecutive_failures"] or 0) >= int(limit)


def classify_stop(conn, task_id, kind, reason, blocker_id, *, human_request=None):
    """Compose continuation recovery with assignment routing without changing human asks."""
    from hermes_cli import kanban_assignment_routing
    from hermes_cli.kanban_continuation_recovery import is_exhausted_budget_block
    from hermes_cli.kanban_db import get_task

    from hermes_cli.kanban_goal_recovery import valid_receipt
    task = get_task(conn, task_id)
    if (kind == 'transient' and human_request is None and task and task.status == 'running'
            and task.human_request is None
            and valid_receipt(conn, task_id, task.current_run_id, reason)):
        return 'transient', 'goal_unachievable', True
    if is_exhausted_budget_block(conn, task_id, kind, reason):
        return "transient", "continuation_exhausted", True
    if kind in {"capability", "assignment"} and (human_request is not None or not blocker_id):
        kind, rekind_reason = kanban_assignment_routing.classify(
            get_task(conn, task_id), kind, reason, human_request=human_request)
        return kind, rekind_reason, False
    return kind, None, False


def supervisor_stop(conn, task_id: str, reason: str, reason_code: str) -> bool:
    """Atomically park an automatic stop for agent investigation, never a human ask."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        changed = conn.execute(
            "UPDATE tasks SET status = 'triage', block_kind = 'transient', human_request = NULL "
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
    from hermes_cli.kanban_wake_check import foreign_copy
    import time

    # Executors still refuse the preclaim guard and record diagnostics, but
    # only the card's home escalates or clears an expired hold.
    if foreign_copy(conn, task_id):
        return False
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
