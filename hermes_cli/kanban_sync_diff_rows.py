"""Portable task projections shared by sync diff and diagnostic evidence. [daveey]"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Optional

from hermes_cli import kanban_db_sync as sync

_DIFF_SKIP = {"current_run_id"}  # compared as run uids instead


def task_rows(conn: sqlite3.Connection, ids: Optional[set[str]] = None) -> dict[str, dict]:
    columns = [c for c in sync.SYNCED_COLUMNS["tasks"] if c not in _DIFF_SKIP]
    present = set(sync.table_columns(conn, "tasks"))
    cols = [c for c in columns if c in present]
    out = {}
    for row in conn.execute(f"SELECT t.id, {', '.join('t.' + c for c in cols)}, r.uid AS run_uid "
                            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id"):
        if ids is None or row["id"] in ids:
            out[row["id"]] = dict(row)
    return out


@contextmanager
def _child_query(conn, sql, *params):
    # Keep execute() overrides and the exact original SQL/decoding path. Only
    # native cursors with the builtin Row wrapper can safely omit that wrapper;
    # custom cursors/factories retain their original behavior.
    from hermes_cli.kanban_replace_guard import _cursor_factory

    rows = conn.execute(sql, *params)
    plain_row = type(rows) in (sqlite3.Cursor, _cursor_factory(sqlite3.Cursor)) and rows.row_factory is sqlite3.Row
    if plain_row:
        rows.row_factory = None
    try:
        yield rows
    finally:
        if plain_row:
            rows.close()


def child_uids(conn, table, task_ids, gateway=None):
    """Full original projections and Python membership, without native Row wrappers.

    Do not SQL-filter foreign rows: even excluded projected text must retain its
    original decoding failures. No connection-wide factory or snapshot changes.
    """
    if table == 'task_links':
        with _child_query(conn, 'SELECT parent_id, child_id FROM task_links') as rows:
            return {(p, f'{p}>{c}') for p, c in rows if c in task_ids}
    if table == 'task_chat_links' and gateway is not None:
        with _child_query(conn,
                "SELECT l.task_id, l.uid FROM task_chat_links l LEFT JOIN tasks t ON t.id = l.task_id WHERE l.uid IS NOT NULL "
                "AND NOT (t.created_on IS NOT NULL AND t.created_on != ?1 AND COALESCE(l.chat_gateway, '') != ?1)",
                (gateway,)) as rows:
            return {(t, u) for t, u in rows if t in task_ids}
    with _child_query(conn, f'SELECT task_id, uid FROM {table} WHERE uid IS NOT NULL') as rows:
        return {(t, u) for t, u in rows if t in task_ids}
