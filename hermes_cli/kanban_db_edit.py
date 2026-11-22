"""``edit_task``: title/body/priority edits and result backfill on a done card, plus the
``hermes kanban edit`` guards that refuse, without a reason, to lower a priority or to raise the
priority of a card the LLM pacing governor holds. Split from ``kanban_db`` (re-exported there). [daveey]"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

_UNSET = object()


def _edit_snapshot_matches(conn, task_id, status, expected_body, home_open_only):
    """Recheck maintenance custody and body CAS under edit_task's write transaction."""
    if home_open_only:
        from hermes_cli.kanban_model_retirement import owns_policy
        if status in ('done', 'archived') or not owns_policy(conn, task_id):
            return False
    if expected_body is not _UNSET:
        stored = conn.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
        return stored['body'] == expected_body
    return True


def _append_edit_comment(conn, task_id, audit_comment):
    if audit_comment is not None:
        _kb.add_comment(conn, task_id, *audit_comment)


class PriorityLowerRefused(ValueError):
    """An edit would lower a card's priority without a reason (``require_lower_reason``). [daveey]"""

    def __init__(self, task_id: str, current: int, requested: int):
        self.task_id, self.current, self.requested = task_id, current, requested
        super().__init__(
            f"{task_id} priority is currently {current}; lowering it to {requested} needs --reason "
            f"(another actor may have raised it since you last read it)"
        )


class PriorityRaisePacedRefused(ValueError):
    """An edit would raise the priority of a ready card the LLM pacing governor holds, without a reason
    (``require_lower_reason``): priority does not lift a pacing hold (defect agent-6f3c6009b5). [daveey]"""

    def __init__(self, task_id: str, current: int, requested: int, pools: str):
        self.task_id, self.current, self.requested, self.pools = task_id, current, requested, pools
        super().__init__(
            f"{task_id} is held by llm_pacing ({pools}); raising its priority {current} -> {requested} does not "
            f"lift a pacing hold, the dispatcher starts it when the pool admits it. Pass --reason to raise it anyway "
            f"(recorded on the reprioritized event)"
        )


def paced_raise_refusal(conn: sqlite3.Connection, task_id: str, current: int,
                        requested: int) -> Optional[PriorityRaisePacedRefused]:
    """The refusal for raising ready card ``task_id`` from ``current`` to ``requested`` while every candidate pool
    is paced or held (``kanban_llm_pacing_dispatch.paced_pools``, the read the stall notice uses); None for a
    non-raise, a card that is not ready, no positive read, or a failing read (fails open). [daveey]"""
    if requested <= current:
        return None
    try:
        if _kb._task_status(conn, task_id) != "ready":
            return None
        from hermes_cli import kanban_llm_pacing_dispatch as pacing
        paces = pacing.paced_pools(conn, task_id)
    except Exception:  # health: allow BLE001 -- the governor read is advisory; the edit proceeds as before
        return None
    if not paces:
        return None
    pools = ", ".join(f"{p.provider} {p.state}" for p in paces)
    return PriorityRaisePacedRefused(task_id, current, requested, pools)


def _refuse_unreasoned_priority(conn: sqlite3.Connection, task_id: str, priority: int) -> None:
    """Raise the refusal for an unreasoned ``priority`` edit: below the stored value, or a raise on a ready card
    the LLM pacing governor holds (:func:`paced_raise_refusal`). Runs inside ``edit_task``'s write txn. [daveey]"""
    row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
    current = int(row[0] or 0) if row else 0
    if priority < current:
        raise PriorityLowerRefused(task_id, current, priority)
    refused = paced_raise_refusal(conn, task_id, current, priority)
    if refused is not None:
        raise refused


def edit_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, priority: Optional[int] = None,
    result: Optional[str] = None, summary: Optional[str] = None,
    metadata: Optional[dict] = None, board: Optional[str] = None,
    priority_reason: Optional[str] = None, require_lower_reason: bool = False,
    priority_bump: Optional[int] = None,
    admission_class: Optional[str] = None, admission_frees: Optional[str] = None,
    expected_body: object = _UNSET, home_open_only: bool = False,
    audit_comment: Optional[tuple[str, str]] = None,
) -> bool:
    """Edit task fields, optionally backfilling a completed task's result.

    ``require_lower_reason`` (the ``hermes kanban edit`` CLI path) raises
    :class:`PriorityLowerRefused` when ``priority`` is below the stored value and no
    ``priority_reason`` is given, and :class:`PriorityRaisePacedRefused` when it is above the stored
    value of a ready card the LLM pacing governor holds; the check runs inside the write transaction, so a
    stale read can't slip past it. ``priority_reason`` lands in the ``reprioritized``
    event payload. Maintenance callers can supply ``expected_body`` for an
    exact stored-body CAS, ``home_open_only`` to recheck nonterminal home custody,
    and ``audit_comment`` to append an explanation in the same transaction. [daveey]
    """
    from hermes_cli.kanban_model_neutral import validate_text
    validate_text(title, body)
    from hermes_cli.kanban_priority import bounded
    if priority_bump is not None and (priority is not None or priority_bump <= 0):
        raise ValueError("priority_bump must be positive and cannot be combined with priority")
    if priority is not None:
        priority = bounded(priority)
    changed_fields = [
        field for field, value in (("title", title), ("body", body), ("priority", priority))
        if value is not None
    ]
    with _kb.write_txn(conn):
        status = _kb._task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        if not _edit_snapshot_matches(conn, task_id, status, expected_body, home_open_only):
            return False
        previous_priority = conn.execute("SELECT priority FROM tasks WHERE id=?", (task_id,)).fetchone()[0] or 0
        if priority_bump is not None:
            priority = bounded(previous_priority + priority_bump)
            changed_fields.append("priority")
        if priority is not None and require_lower_reason and not (priority_reason or "").strip():
            _refuse_unreasoned_priority(conn, task_id, priority)
        assignments = []
        params = []
        from hermes_cli.kanban_admission_class import edit_fields
        admission = edit_fields(conn, task_id, admission_class, admission_frees)
        changed_fields.extend(admission)
        for field, value in (("title", title), ("body", body), ("priority", priority), *admission.items()):
            if value is not None:
                assignments.append(f"{field} = ?")
                params.append(value)
        if result is not None:
            assignments.append("result = ?")
            params.append(result)
            changed_fields.append("result")
        if not assignments:
            return False
        conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
            (*params, task_id),
        )
        if priority is not None:
            payload: dict = {"priority": priority, "previous_priority": previous_priority}
            if priority_bump is not None:
                payload["relative_bump"] = priority_bump
                payload["applied_delta"] = priority - previous_priority
            if priority_reason and priority_reason.strip():
                payload["reason"] = priority_reason.strip()
            _kb._append_event(conn, task_id, "reprioritized", payload)
        if result is None:
            non_priority_fields = [field for field in changed_fields if field != "priority"]
            if non_priority_fields:
                _kb._append_event(conn, task_id, "edited", {"fields": non_priority_fields})
        else:
            handoff_summary = summary if summary is not None else result
            changed_fields.append("summary")
            if metadata is not None:
                changed_fields.append("metadata")
            run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
            from hermes_cli.kanban_completion_receipt import check_summary_edit
            check_summary_edit(conn, task_id, None if run is None else int(run["id"]), handoff_summary)
            if metadata is not None:
                from hermes_cli.kanban_completion_receipt import keep_admitted_receipt
                metadata = keep_admitted_receipt(conn, None if run is None else int(run["id"]), metadata)
            if run is None:
                run_id = _kb._synthesize_ended_run(
                    conn, task_id, outcome="completed", summary=handoff_summary, metadata=metadata,
                )
            else:
                run_id = int(run["id"])
                conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (handoff_summary, run_id))
                if metadata is not None:
                    conn.execute(
                        "UPDATE task_runs SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False), run_id),
                    )
            _kb._append_event(
                conn, task_id, "edited",
                {
                    "fields": ["result", "summary"] + (["metadata"] if metadata is not None else []),
                    "result_len": len(result) if result else 0,
                    "summary": _kb._first_line(handoff_summary, 400) or None,
                },
                run_id=run_id,
            )
        _append_edit_comment(conn, task_id, audit_comment)
    _kb.notify_task_updated(conn, task_id, changed_fields, board=board)
    return True


# Late-bound origin namespace, imported LAST so this module is fully populated before
# ``kanban_db`` imports from it (import-first cycle, test_kanban_db_split_imports).
from hermes_cli import kanban_db as _kb
