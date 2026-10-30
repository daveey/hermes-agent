"""Evidence gate for a Kanban completion, before the lifecycle write transaction."""
from __future__ import annotations

import sqlite3
from typing import Optional


def gate_empty_completion(conn: sqlite3.Connection, task_id: str, *,
                          result: Optional[str], summary: Optional[str]) -> None:
    """Require substantive evidence except when approving a reviewed card."""
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] == "review":
        return
    if kb._substantive_text(result) or kb._substantive_text(summary) or kb._substantive_text(row["result"]):
        return
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "completion_blocked_empty_result", {
            "result_preview": kb._first_line(result, 200) or None,
            "summary_preview": kb._first_line(summary, 200) or None,
        })
    raise kb.EmptyCompletionError(task_id)
