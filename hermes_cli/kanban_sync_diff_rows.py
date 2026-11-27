"""Portable task projections shared by sync diff and diagnostic evidence. [daveey]"""
from __future__ import annotations

import sqlite3
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
