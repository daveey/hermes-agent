"""Manual promotion primitive; surface policy arrives as an optional precondition. [daveey]"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False,
    precondition: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None,
) -> tuple[bool, Optional[str]]:
    """Promote todo/blocked/triage, or a scheduled spend stop, after caller and parent checks.

    System callers omit the precondition. Manual boundaries check unread comments
    under the write lock; dry runs validate without writing anything.
    """
    cur_status = _kb._task_status(conn, task_id)
    if cur_status is None:
        return False, f"task {task_id} not found"
    from hermes_cli.kanban_spend_decision import pending
    if cur_status not in _kb._PROMOTABLE_STATUSES and not (cur_status == "scheduled" and pending(conn, task_id)):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo', 'blocked' or 'triage'"
        )
    # No override: claim_task re-checks parents regardless of who promoted the card.
    unsatisfied = [pid for pid, st in _kb.parent_states(conn, task_id) if st not in ("done", "archived")]
    if unsatisfied:
        return False, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents or drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`)"
        )
    if dry_run:
        if precondition and (refusal := precondition(conn)):
            return False, refusal
        return True, None
    with _kb.write_txn(conn):
        if precondition and (refusal := precondition(conn)):
            return False, refusal
        # A scheduled exception authorizes only an unresolved spend decision, rechecked under the writer lock.
        if cur_status == "scheduled" and not pending(conn, task_id):
            return False, f"task {task_id} spend decision changed during promotion"
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status = ?", (task_id, cur_status),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _kb._append_event(conn, task_id, "promoted_manual", {
            "actor": actor, "reason": reason,
            **({"from_status": cur_status} if cur_status != "todo" else {}),
        })
    return True, None


# Late-bound facade keeps existing monkeypatch and direct-caller contracts.
from hermes_cli import kanban_db as _kb
