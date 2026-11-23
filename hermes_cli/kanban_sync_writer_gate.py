"""Monotone, opt-in local Board writer admission. [daveey]

Revision 1 promised only the DB fence. Revision 2 additionally identifies the
sender build with audited pending provenance and full public sender exclusion;
it does NOT certify original captures or establish migration readiness. Installing
this code does not activate or raise a gate. The migration owner must first drain
old processes AND in-flight sends; SQLite cannot revoke bytes already in memory.
Active gates survive sync detach/rollback. No downgrade/removal API is provided.
"""
from __future__ import annotations

import sqlite3

REVISION = 2
_TABLE = "replay_writer_gate1"
_FUNCTION = "replay_writer_revision"


def register(conn: sqlite3.Connection) -> None:
    """Declare the running writer's admission revision on every new connection."""
    conn.create_function(_FUNCTION, 0, lambda: REVISION)


def activate(conn: sqlite3.Connection) -> None:
    """Persist/raise the minimum to this revision inside an exclusive write boundary.

    This is the local downgrade prerequisite, not migration readiness or authority
    to certify provenance. No production caller activates it yet. Trigger names
    deliberately do not share the removable ``kanban_sync_%`` prefix. Existing
    connections see the new schema before their next statement. Missing function
    is a hard SQLite refusal, so older cores cannot silently opt out.
    """
    if not conn.in_transaction:
        raise ValueError("writer gate activation requires a write transaction")
    conn.execute("SAVEPOINT replay_writer_gate_activation")
    try:
        _install(conn)
    except BaseException:
        # Roll back even if the outer caller catches the failure and commits.
        conn.execute("ROLLBACK TO replay_writer_gate_activation")
        conn.execute("RELEASE replay_writer_gate_activation")
        raise
    else:
        conn.execute("RELEASE replay_writer_gate_activation")


def _install(conn: sqlite3.Connection) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
        id INTEGER PRIMARY KEY CHECK(id=1),
        minimum INTEGER NOT NULL CHECK(typeof(minimum)='integer' AND minimum>=1))""")
    conn.execute(f"""INSERT INTO {_TABLE}(id,minimum)
        SELECT 1,? WHERE NOT EXISTS(SELECT 1 FROM {_TABLE} WHERE id=1)""", (REVISION,))
    # A revision-1 gate already exists on some images. Merely registering the new
    # connection or reinstalling IF NOT EXISTS triggers must not silently admit it.
    conn.execute(f"UPDATE {_TABLE} SET minimum=? WHERE id=1 AND minimum<?", (REVISION, REVISION))
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in names:
        quoted = '"' + table.replace('"', '""') + '"'
        for action in ("INSERT", "UPDATE", "DELETE"):
            name = '"replay_writer1_' + table.replace('"', '""') + '_' + action.lower() + '"'
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {quoted}
                BEGIN SELECT CASE WHEN {_FUNCTION}() < (SELECT minimum FROM {_TABLE} WHERE id=1)
                    THEN RAISE(ABORT, 'replay writer revision too old') END; END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS replay_writer1_minimum_monotone BEFORE UPDATE ON {_TABLE}
        WHEN NEW.minimum < OLD.minimum OR NEW.id IS NOT OLD.id
        BEGIN SELECT RAISE(ABORT, 'replay writer minimum cannot decrease'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS replay_writer1_minimum_insert BEFORE INSERT ON {_TABLE}
        WHEN EXISTS(SELECT 1 FROM {_TABLE}) AND
            NOT EXISTS(SELECT 1 FROM {_TABLE} WHERE id=NEW.id AND minimum=NEW.minimum)
        BEGIN SELECT RAISE(ABORT, 'replay writer minimum cannot be replaced'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS replay_writer1_minimum_retained BEFORE DELETE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, 'replay writer minimum cannot be removed'); END""")
