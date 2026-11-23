"""Column names of kanban tables in ONE SQLite result row. [daveey]

CPython's sqlite3 releases the GIL around every statement prepare and every row step. In the Desktop backend (one
GIL process, ~190 threads) each re-acquire can wait out the 5 ms switch interval, so ``PRAGMA table_info(tasks)``
(one row per column: 59 on the hub board) took ~140 ms beside ONE busy thread instead of 0.06 ms, and the schema
probes of the session-task and linked-card readers were the top lines of the backend's profile.

``pragma_table_info`` aggregated with ``json_group_array`` returns the same names, built inside SQLite, in one row:
one statement for any number of tables. It is the same lookup as the PRAGMA (temp, main, then attached schemas; an
empty set for a missing table), read on the caller's connection and in its snapshot. So there is no cache and no
cache key that could miss a migration, a rebuilt replica or another file at the same path: a memo would still need
one statement to prove its key (``schema_version``), which is all this costs. ``machine_metrics.table_columns``
uses the same one-row form.
"""

from __future__ import annotations

import json
import sqlite3

_ONE = "(SELECT json_group_array(name) FROM pragma_table_info(?))"


def tables_columns(conn: sqlite3.Connection, *tables: str) -> tuple[frozenset[str], ...]:
    """``PRAGMA table_info(<table>)`` column names for each of ``tables``, in one statement and one row."""
    if not tables:
        return ()
    cur = conn.cursor()
    try:
        cur.row_factory = None  # whatever the connection's factory, index the single row positionally
        row = cur.execute("SELECT " + ", ".join(_ONE for _ in tables), tables).fetchone()
    finally:
        cur.close()
    return tuple(frozenset(json.loads(cell)) for cell in row)


def table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    """``{r[1] for r in conn.execute('PRAGMA table_info(<table>)')}`` without one result row per column."""
    return tables_columns(conn, table)[0]
