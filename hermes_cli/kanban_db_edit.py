"""``edit_task``: title/body/priority edits and result backfill on a done card, plus the
``hermes kanban edit`` guard that refuses to lower a priority without a reason. Split from
``kanban_db`` (re-exported there). [daveey]"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


class PriorityLowerRefused(ValueError):
    """An edit would lower a card's priority without a reason (``require_lower_reason``). [daveey]"""

    def __init__(self, task_id: str, current: int, requested: int):
        self.task_id, self.current, self.requested = task_id, current, requested
        super().__init__(
            f"{task_id} priority is currently {current}; lowering it to {requested} needs --reason "
            f"(another actor may have raised it since you last read it)"
        )


def edit_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, priority: Optional[int] = None,
    result: Optional[str] = None, summary: Optional[str] = None,
    metadata: Optional[dict] = None, board: Optional[str] = None,
    priority_reason: Optional[str] = None, require_lower_reason: bool = False,
) -> bool:
    """Edit task fields, optionally backfilling a completed task's result.

    ``require_lower_reason`` (the ``hermes kanban edit`` CLI path) raises
    :class:`PriorityLowerRefused` when ``priority`` is below the stored value and no
    ``priority_reason`` is given; the check runs inside the write transaction, so a
    stale read can't slip past it. ``priority_reason`` lands in the ``reprioritized``
    event payload. [daveey]
    """
    changed_fields = [
        field for field, value in (("title", title), ("body", body), ("priority", priority))
        if value is not None
    ]
    with _kb.write_txn(conn):
        status = _kb._task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        if priority is not None and require_lower_reason and not (priority_reason or "").strip():
            row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
            current = int(row[0] or 0) if row else 0
            if priority < current:
                raise PriorityLowerRefused(task_id, current, priority)
        assignments = []
        params = []
        for field, value in (("title", title), ("body", body), ("priority", priority)):
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
            payload: dict = {"priority": priority}
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
    _kb.notify_task_updated(conn, task_id, changed_fields, board=board)
    return True


# Late-bound origin namespace, imported LAST so this module is fully populated before
# ``kanban_db`` imports from it (import-first cycle, test_kanban_db_split_imports).
from hermes_cli import kanban_db as _kb
