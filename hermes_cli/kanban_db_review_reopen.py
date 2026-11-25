"""``reopen_review_task``: ``review`` -> ``ready``/``todo`` (split out of ``kanban_db``). [daveey]

Late-bound via ``_kb`` (import-cycle breaking) so monkeypatching ``kanban_db`` helpers in
tests still reaches this code. Re-exported from ``kanban_db``.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any


def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``review`` -> ``ready``/``todo`` so the implementer re-runs on the new
    comments; restores the implementer from the ``review_requested`` event.
    Preserves ``consecutive_failures`` and the block loop counter (review is
    not a block; only :func:`complete_task` clears them)."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_home_transfer import frozen
    now = int(time.time())
    with _kb.write_txn(conn):
        if frozen(conn, task_id):
            return False
        task = _kb.get_task(conn, task_id)
        if task is None or task.status != "review":
            return False
        review_event = _kb._latest_event(conn, task_id, "review_requested")
        handoff = _kb._json_dict(_kb._row_get(review_event, "payload"))
        implementer = _kb._nonblank_str(handoff.get("implementer"))
        if implementer:
            from hermes_cli.kanban_capabilities import validate_assignment

            validate_assignment(implementer, requires=task.requires,
                                workspace_kind=task.workspace_kind, workspace_path=task.workspace_path, skills=task.skills)
        _kb._reclaim_dangling_run(
            conn, task_id, statuses=("review",), now=now,
            note="invariant recovery on review reopen",
        )
        new_status = _kb._landing_status_after_parents(conn, task_id)
        params: tuple[Any, ...] = (new_status, *((implementer,) if implementer else ()), task_id)
        cur = conn.execute(
            # consecutive_failures deliberately PRESERVED: review reopen is not
            # a success signal; only complete_task resets the breaker (#35072).
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            + (", assignee = ?" if implementer else "")
            + " WHERE id = ? AND status = 'review'",
            params,
        )
        if cur.rowcount != 1:
            return False
        payload: dict[str, Any] = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        _kb._append_event(
            conn, task_id, "review_reopened", payload if payload != {"status": "ready"} else None,
        )
        return True
