"""Schema v1 for the account source-custody authority (``pins.sqlite3``).

Inert WIP: only :mod:`hermes_cli.kanban_source_pin_store` uses it. Runtime never
runs this DDL; only explicit setup does, in one transaction with the meta row.
"""
from __future__ import annotations

import sqlite3

VERSION = 1
TABLES = ("meta", "operations", "units", "imports", "recovery")

DDL = (
    "CREATE TABLE meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    " version INTEGER NOT NULL, store_uuid TEXT NOT NULL, created_at INTEGER NOT NULL)",
    "CREATE TABLE operations(op_id TEXT PRIMARY KEY, lookup_key TEXT UNIQUE NOT NULL,"
    " board_uuid TEXT NOT NULL, task_id TEXT NOT NULL, source_host TEXT NOT NULL,"
    " original_run_json TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,"
    " scope_json TEXT NOT NULL, phase TEXT NOT NULL, last_attempted INTEGER,"
    " projection_revision INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE units(op_id TEXT, unit_id TEXT, revision INTEGER, kind TEXT, state TEXT,"
    " payload_json TEXT, PRIMARY KEY(op_id, unit_id))",
    "CREATE TABLE imports(input_key TEXT PRIMARY KEY NOT NULL, cursor_json TEXT NOT NULL,"
    " complete INTEGER NOT NULL)",
    "CREATE TABLE recovery(op_id TEXT PRIMARY KEY, episode INTEGER, key TEXT UNIQUE,"
    " destination TEXT, task_id TEXT, state TEXT, last_attempted INTEGER)",
    "CREATE INDEX operations_task ON operations(board_uuid, task_id)",
)

# (cid-free) column fingerprints: name, declared type, notnull, pk position.
_COLUMNS = {
    "meta": [("singleton", "INTEGER", 0, 1), ("version", "INTEGER", 1, 0),
             ("store_uuid", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0)],
    "operations": [("op_id", "TEXT", 0, 1), ("lookup_key", "TEXT", 1, 0),
                   ("board_uuid", "TEXT", 1, 0), ("task_id", "TEXT", 1, 0),
                   ("source_host", "TEXT", 1, 0), ("original_run_json", "TEXT", 1, 0),
                   ("kind", "TEXT", 1, 0), ("revision", "INTEGER", 1, 0),
                   ("scope_json", "TEXT", 1, 0), ("phase", "TEXT", 1, 0),
                   ("last_attempted", "INTEGER", 0, 0),
                   ("projection_revision", "INTEGER", 1, 0)],
    "units": [("op_id", "TEXT", 0, 1), ("unit_id", "TEXT", 0, 2), ("revision", "INTEGER", 0, 0),
              ("kind", "TEXT", 0, 0), ("state", "TEXT", 0, 0), ("payload_json", "TEXT", 0, 0)],
    "imports": [("input_key", "TEXT", 1, 1), ("cursor_json", "TEXT", 1, 0),
                ("complete", "INTEGER", 1, 0)],
    "recovery": [("op_id", "TEXT", 0, 1), ("episode", "INTEGER", 0, 0), ("key", "TEXT", 0, 0),
                 ("destination", "TEXT", 0, 0), ("task_id", "TEXT", 0, 0),
                 ("state", "TEXT", 0, 0), ("last_attempted", "INTEGER", 0, 0)],
}


def create(conn: sqlite3.Connection, store_uuid: str, created_at: int) -> None:
    """Create every table and the single meta row. Caller owns the transaction."""
    for statement in DDL:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO meta(singleton, version, store_uuid, created_at) VALUES(1, ?, ?, ?)",
        (VERSION, store_uuid, created_at),
    )


def verify(conn: sqlite3.Connection) -> str:
    """Return the store UUID of an intact v1 store; raise ``ValueError`` otherwise."""
    objects = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall()
    expected = {("table", name) for name in TABLES} | {("index", "operations_task")}
    if {tuple(row) for row in objects} != expected or len(objects) != len(expected):
        raise ValueError("source-custody schema objects do not match v1")
    for table, columns in _COLUMNS.items():
        found = [(r[1], r[2].upper(), r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")]
        if found != columns:
            raise ValueError(f"source-custody table {table} does not match v1")
    rows = conn.execute("SELECT singleton, version, store_uuid FROM meta").fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != VERSION:
        raise ValueError("source-custody meta row is missing or has another version")
    store_uuid = rows[0][2]
    if not isinstance(store_uuid, str) or len(store_uuid) != 32 or any(
        c not in "0123456789abcdef" for c in store_uuid
    ):
        raise ValueError("source-custody store uuid is malformed")
    return store_uuid


def is_pristine(conn: sqlite3.Connection) -> bool:
    """True when no content table holds a row (a freshly created, never used store)."""
    return all(
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
        for table in TABLES if table != "meta"
    )
