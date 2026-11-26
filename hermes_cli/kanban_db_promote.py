"""Manual promotion primitive; surface policy arrives as an optional precondition. [daveey]"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional


def _promotion_status(conn: sqlite3.Connection, task_id: str, *, admission=None) -> tuple[Optional[str], Optional[str]]:
    from hermes_cli.kanban_spend_decision import execution_refusal, pending
    if admission is None:
        refusal = execution_refusal(conn, task_id)
    else:
        from hermes_cli.kanban_release_assemble import _admission_refusal
        refusal = _admission_refusal(conn, task_id, admission)
    if refusal:
        return None, refusal
    status = _kb._task_status(conn, task_id)
    if status is None:
        return None, f"task {task_id} not found"
    if status not in _kb._PROMOTABLE_STATUSES and not (status in ("scheduled", "ready") and pending(conn, task_id)):
        return None, (
            f"task {task_id} is {status!r}; promote only applies to "
            f"'todo', 'blocked' or 'triage', or 'scheduled'/'ready' with a pending spend decision"
        )
    unsatisfied = [pid for pid, st in _kb.parent_states(conn, task_id) if st not in ("done", "archived")]
    if unsatisfied:
        return None, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents or drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`)"
        )
    return status, None


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False,
    precondition: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None,
) -> tuple[bool, Optional[str]]:
    """Promote after caller and parent checks, owning its write transaction.

    Manual boundaries check unread comments under the write lock; dry runs
    validate without writing. A ready/scheduled exception decides only a pending
    spend stop. Missing HOME history must be recovered, never inferred cleared.
    """
    status, refusal = _promotion_status(conn, task_id)
    if refusal:
        return False, refusal
    if dry_run:
        if precondition and (refusal := precondition(conn)):
            return False, refusal
        return True, None
    with _kb.write_txn(conn):
        return promote_in_txn(conn, task_id, actor=actor, reason=reason, precondition=precondition,
                              expected_status=status)


def promote_in_txn(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    precondition: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None,
    expected_status: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Promotion body for an existing writer; no commit or authority exemption.

    The composing caller MUST raise on a returned refusal after earlier writes,
    so its outer transaction rolls back. Preconditions also belong to that outer
    transaction. No network or lifecycle wrapper may run inside this body.
    """
    return _promote_in_txn(conn, task_id, actor=actor, reason=reason, precondition=precondition,
                           expected_status=expected_status)


def _promote_in_txn(conn, task_id, *, actor, reason=None, precondition=None, expected_status=None, admission=None):
    """Private composition seam; public promotion never accepts an admission."""
    if not conn.in_transaction:
        raise RuntimeError("promotion body requires an outer write transaction")
    from hermes_cli.kanban_spend_decision import pending
    status, refusal = _promotion_status(conn, task_id, admission=admission)
    if refusal:
        return False, refusal
    if expected_status is not None and status != expected_status:
        return False, f"task {task_id} status changed during promotion"
    if precondition and (refusal := precondition(conn)):
        return False, refusal
    if status in ("scheduled", "ready") and not pending(conn, task_id):
        return False, f"task {task_id} spend decision changed during promotion"
    upd = conn.execute(
        "UPDATE tasks SET status = 'ready' "
        "WHERE id = ? AND status IN ('todo', 'blocked', 'triage', 'scheduled', 'ready') AND status = ?",
        (task_id, status),
    )
    if upd.rowcount != 1:
        return False, f"task {task_id} status changed during promotion"
    from hermes_cli import kanban_spend_budget as budget
    # Restricted history remains cumulative after authorized scope renewal.
    window = ({"spend_budget": budget.card_limits(conn, task_id, budget.limits())._asdict()}
              if budget.limit_window(conn, task_id) == 'next_promote' else {})
    _kb._append_event(conn, task_id, "promoted_manual", {
        "actor": actor, "reason": reason,
        **({"from_status": status} if status != "todo" else {}), **window,
    })
    return True, None


# Late-bound facade keeps existing monkeypatch and direct-caller contracts.
from hermes_cli import kanban_db as _kb
