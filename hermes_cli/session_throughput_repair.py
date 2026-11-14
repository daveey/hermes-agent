"""Disable provably owned triggers stranded by partial protocol-table loss."""
from __future__ import annotations

import sqlite3


def disable_stranded(conn: sqlite3.Connection) -> bool | None:
    """True disabled, False retryable error, None foreign/not a partial layout.

    Preserve all surviving data. A partial layout remains full-SQL-only; do not
    reconstruct a revision history from incomplete artifacts. Future schemas are
    untouched when identifiable; absent revision metadata is proven only by the
    exact current table/trigger definitions that remain.
    """
    from hermes_cli import session_throughput_revision as rev

    try:
        tables = rev._master(conn, "table")
        present = set(rev._TABLE_SQL) & tables.keys()
        if not 0 < len(present) < len(rev._TABLE_SQL):
            return None
        objects = {r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
        if any(name in objects for name in rev._TABLE_SQL if name not in present):
            return None
        if any(tables[name] != rev._TABLE_SQL[name] for name in present):
            return None
        if "throughput_changes" in present and rev._master(conn, "index").get(rev._INDEX_NAME) != rev._INDEX_SQL:
            return None
        if not rev._triggers_exact(conn):
            return None
        if "throughput_revision" in present:
            rows = conn.execute('SELECT version FROM throughput_revision').fetchall()
            if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != rev.SCHEMA_VERSION:
                return None
        conn.execute('SAVEPOINT throughput_disable')
    except sqlite3.Error:
        return False
    try:
        rev.drop_triggers(conn)
        conn.execute('RELEASE throughput_disable')
    except sqlite3.Error:
        try:
            conn.execute('ROLLBACK TO throughput_disable')
            conn.execute('RELEASE throughput_disable')
        except sqlite3.Error:
            pass
        return False
    return True
