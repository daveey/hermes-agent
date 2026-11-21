"""Subscription-only revision, independent of the annotation's global counter.

Installed by writable board initialization; readers never repair schema. Exact SQL
coverage is checked in the caller's snapshot. Missing/older/partial coverage
means a literal read; no coverage proof is reused across reader/file generations.
"""
from __future__ import annotations

import sqlite3

TABLE = "kanban_notify_revision"
PREFIX = "kanban_notify_rev_"
COLUMNS = ("chat_id", "task_id", "platform", "thread_id", "last_event_id", "inflight_from")
DDL = f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
BUMP = f"UPDATE {TABLE} SET revision = revision + 1 WHERE id = 1;"
_CHANGED = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in COLUMNS)
TRIGGERS = {
    f"{PREFIX}{op.lower()}": (
        f"CREATE TRIGGER {PREFIX}{op.lower()} AFTER {op} ON kanban_notify_subs"
        + (f" WHEN {_CHANGED}" if op == "UPDATE" else "")
        + f" BEGIN {BUMP} END"
    ) for op in ("INSERT", "DELETE", "UPDATE")
}


def _schema(conn):
    return dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name = ? OR (type = 'trigger' AND name GLOB ?)",
        (TABLE, PREFIX + "*")))


def ensure_revision(conn: sqlite3.Connection) -> None:
    """Repair atomically and bump across any uncovered interval; intact opens do not write."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
    if not set(COLUMNS) <= columns:
        return  # older schema: the reader remains on literal subscription reads
    wanted = {TABLE: DDL, **TRIGGERS}
    found = _schema(conn)
    if found == wanted and conn.execute(f"SELECT 1 FROM {TABLE} WHERE id = 1").fetchone():
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        found = _schema(conn)
        if TABLE not in found:
            conn.execute(DDL)
        elif found[TABLE] != DDL:
            return  # foreign schema: never silently replace its data or certify it
        had_row = conn.execute(f"SELECT 1 FROM {TABLE} WHERE id = 1").fetchone() is not None
        conn.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES (1, 0)")
        conn.execute(BUMP)
        for name in set(found) - {TABLE} - set(TRIGGERS):
            conn.execute(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
        for name, sql in TRIGGERS.items():
            if found.get(name) != sql or not had_row:
                # A lost counter may restart at a previously cached value. Change
                # schema generation too, so an unseen gap cannot certify old rows.
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(sql)


def read_revision(conn: sqlite3.Connection, identity: tuple) -> tuple | None:
    """Read inside the subscription snapshot, verifying coverage and schema afresh."""
    schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    if _schema(conn) != {TABLE: DDL, **TRIGGERS}:
        return None
    row = conn.execute(f"SELECT revision FROM {TABLE} WHERE id = 1").fetchone()
    if row is None:
        return None
    return (*identity, int(row[0]), schema)
