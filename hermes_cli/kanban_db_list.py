"""``list_tasks`` / ``list_task_rows`` (split out of ``kanban_db``). [daveey]

Late-bound via ``_kb`` (import-cycle breaking) so monkeypatching ``kanban_db`` helpers in
tests still reaches this code. Re-exported from ``kanban_db``.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional, Sequence


def list_tasks(
    conn: sqlite3.Connection, *, assignee: Optional[str] = None, status: Optional[str] = None,
    tenant: Optional[str] = None, session_id: Optional[str] = None, include_archived: bool = False,
    limit: Optional[int] = None, order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None, current_step_key: Optional[str] = None,
    extra_where: str = "", extra_params: Sequence[Any] = (), parse: Optional[Callable[[sqlite3.Row], _kb.Task]] = None,
) -> list[_kb.Task]:
    """``parse`` (default ``Task.from_row``) lets the board reuse a Task parsed from identical row values."""
    return [(parse or _kb.Task.from_row)(r) for r in list_task_rows(
        conn, assignee=assignee, status=status, tenant=tenant, session_id=session_id,
        include_archived=include_archived, limit=limit, order_by=order_by,
        workflow_template_id=workflow_template_id, current_step_key=current_step_key,
        extra_where=extra_where, extra_params=extra_params)]


def list_task_rows(
    conn: sqlite3.Connection, *, assignee: Optional[str] = None, status: Optional[str] = None,
    tenant: Optional[str] = None, session_id: Optional[str] = None, include_archived: bool = False,
    limit: Optional[int] = None, order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None, current_step_key: Optional[str] = None,
    columns: str = "*", extra_where: str = "", extra_params: Sequence[Any] = (),
) -> list[sqlite3.Row]:
    """``list_tasks``'s raw rows (``columns`` selected, ``extra_where`` ANDed in): the board
    re-parses only rows whose values changed since its last read."""
    if status is not None and status not in _kb.VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_kb.VALID_STATUSES)}")
    query = f"SELECT {columns} FROM tasks WHERE 1=1"
    params: list[Any] = []
    for col, val in (
        ("assignee", _kb._canonical_assignee(assignee)), ("status", status), ("tenant", tenant),
        ("session_id", session_id), ("workflow_template_id", workflow_template_id),
        ("current_step_key", current_step_key),
    ):
        if val is not None:
            query += f" AND {col} = ?"
            params.append(val)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if extra_where:
        query += f" AND ({extra_where})"
        params.extend(extra_params)
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in _kb.VALID_SORT_ORDERS:
            raise ValueError(f"order_by must be one of {sorted(_kb.VALID_SORT_ORDERS.keys())}")
        query += f" ORDER BY {_kb.VALID_SORT_ORDERS[order_by]}"
    else:  # the dispatcher's lane order: a card others wait on first among its effective priority [daveey]
        query += f" ORDER BY {_kb.DISPATCH_ORDER}"
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, params).fetchall()


from hermes_cli import kanban_db as _kb
