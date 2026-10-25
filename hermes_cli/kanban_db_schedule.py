"""``schedule_task``: park a card in ``scheduled`` (split out of ``kanban_db``). [daveey]

Late-bound via ``_kb`` (import-cycle breaking) so monkeypatching ``kanban_db`` helpers in
tests still reaches this code. Re-exported from ``kanban_db``.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db as _kb


def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None, wake_check: Optional[str] = None,
    check_every: Optional[int] = None, clock_only: bool = False,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable) until its wake
    (``SCHEDULED_UNTIL=`` in the reason, else ``kanban_timed_park.DEFAULT_WAKE_SECONDS``) or an
    ``unblock_task``. Refused (False) while the card waits on a human answer: ``scheduled`` is
    not on any needs-me surface, so such a park hid the ask (t_6feb6afd). [daveey]"""
    with _kb.write_txn(conn):
        if _kb._awaiting_human_answer(conn, task_id):
            return False
        from hermes_cli.kanban_timed_park import scheduled_payload
        from hermes_cli import kanban_wake_check as kwc
        payload = scheduled_payload(reason)
        wake_check, check_every = kwc.validate_park(
            wake_at=payload["wake_at"], wake_check=wake_check,
            check_every=check_every, clock_only=clock_only)
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _kb._end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        _kb._append_event(conn, task_id, "scheduled", {
            **payload, **kwc.park_fields(wake_check, check_every, clock_only),
        }, run_id=run_id)
        kwc.set_check(conn, task_id, wake_check, check_every)
        return True
