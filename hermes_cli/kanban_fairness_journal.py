"""Opt-in SQLite change journal for the queue-fairness capture. [daveey]

``arm`` installs, in one write transaction, two feature-owned tables, an opening snapshot of every open card
and ``kanban_fairness_*`` triggers on ``tasks`` and ``task_events``. From then on every row version that
changes a queue-relevant field, and every lifecycle event, is appended with the database host's wall clock;
changes a sync apply writes are recorded too (the sync capture triggers skip those). Nothing here runs unless
an operator arms it, and no scheduler code reads it.

A journal row is written inside the writer's own transaction, so a rollback removes it and the writer never
waits on a file. The journal is bounded: past ``max_records`` or ``max_bytes`` the triggers only count what
they dropped, which the reader treats as a coverage gap. ``seal`` stops the capture and removes the triggers;
the rows stay until ``clear`` so they can be drained again.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Iterator, Optional

from hermes_cli.kanban_fairness_schema import EVENT_KINDS, MAX_BYTES, MAX_RECORDS, TASK_FIELDS

JOURNAL, SESSION = "kanban_fairness_journal", "kanban_fairness_session"
_NOW_MS = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
_WATCHED = ("status", "priority", "idempotency_key", "assignee", "ready_since", "lease_holder")
_SCHEMA = (
    f"CREATE TABLE {JOURNAL} (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
    "wall_ms INTEGER NOT NULL, task_id TEXT NOT NULL, op TEXT, fields TEXT NOT NULL)",
    f"CREATE TABLE {SESSION} (id INTEGER PRIMARY KEY CHECK (id = 1), capture_id TEXT NOT NULL, "
    "armed_ms INTEGER NOT NULL, sealed_ms INTEGER, max_records INTEGER NOT NULL, max_bytes INTEGER NOT NULL, "
    "stored INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0, "
    "first_drop_ms INTEGER, last_drop_ms INTEGER, admit INTEGER NOT NULL DEFAULT 1)",
)


class JournalError(RuntimeError):
    """The journal is not in the state the caller needs (already armed, never armed, sealed)."""


def _fields_sql(columns: list[str], ref: str) -> str:
    return "json_object(" + ", ".join(f"'{c}', {ref}.{c}" for c in columns) + ")"


def _append(kind: str, task_id: str, op: Optional[str], fields: str) -> str:
    """Count the change, then store it only while the session is open and under both bounds."""
    # The bound is on the UTF-8 bytes the row would add (not SQLite's character length), checked before admission.
    full = f"(stored >= max_records OR bytes + length(CAST({fields} AS BLOB)) > max_bytes)"
    op_sql = "NULL" if op is None else f"'{op}'"
    return (
        f"UPDATE {SESSION} SET admit = NOT {full}, dropped = dropped + {full}, "
        f"first_drop_ms = CASE WHEN {full} THEN COALESCE(first_drop_ms, {_NOW_MS}) ELSE first_drop_ms END, "
        f"last_drop_ms = CASE WHEN {full} THEN {_NOW_MS} ELSE last_drop_ms END, "
        f"bytes = bytes + CASE WHEN {full} THEN 0 ELSE length(CAST({fields} AS BLOB)) END, stored = stored + NOT {full} "
        "WHERE id = 1 AND sealed_ms IS NULL; "
        f"INSERT INTO {JOURNAL} (kind, wall_ms, task_id, op, fields) "
        f"SELECT '{kind}', {_NOW_MS}, {task_id}, {op_sql}, {fields} FROM {SESSION} "
        "WHERE id = 1 AND sealed_ms IS NULL AND admit = 1;"
    )


def _triggers(conn: sqlite3.Connection) -> dict[str, str]:
    present = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    columns = [c for c in TASK_FIELDS if c in present]
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _WATCHED if c in present)
    kinds = ", ".join(f"'{k}'" for k in EVENT_KINDS)
    event = "json_object('kind', NEW.kind, 'created_at', NEW.created_at)"
    return {
        "kanban_fairness_tasks_ins": "CREATE TRIGGER kanban_fairness_tasks_ins AFTER INSERT ON tasks BEGIN "
        + _append("task", "NEW.id", "insert", _fields_sql(columns, "NEW")) + " END",
        "kanban_fairness_tasks_upd": "CREATE TRIGGER kanban_fairness_tasks_upd AFTER UPDATE ON tasks "
        f"WHEN {changed} BEGIN " + _append("task", "NEW.id", "update", _fields_sql(columns, "NEW")) + " END",
        "kanban_fairness_tasks_del": "CREATE TRIGGER kanban_fairness_tasks_del AFTER DELETE ON tasks BEGIN "
        + _append("task", "OLD.id", "delete", "'{}'") + " END",
        "kanban_fairness_events_ins": "CREATE TRIGGER kanban_fairness_events_ins AFTER INSERT ON task_events "
        f"WHEN NEW.kind IN ({kinds}) BEGIN " + _append("event", "NEW.task_id", None, event) + " END",
    }


def _has(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _drop_triggers(conn: sqlite3.Connection) -> None:
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'kanban_fairness_*'").fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def identity(conn: sqlite3.Connection) -> dict:
    """The database file behind ``conn``: a replaced file (a replica rebuild) has another inode."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    stat = os.stat(path)
    return {"path": path, "dev": stat.st_dev, "ino": stat.st_ino}


def status(conn: sqlite3.Connection) -> Optional[dict]:
    """The session row plus trigger presence, or None on a database that was never armed."""
    if not _has(conn, SESSION):
        return None
    row = conn.execute(
        f"SELECT capture_id, armed_ms, sealed_ms, max_records, max_bytes, stored, bytes, dropped, "
        f"first_drop_ms, last_drop_ms FROM {SESSION} WHERE id = 1").fetchone()
    if row is None:
        return None
    names = ("capture_id", "armed_ms", "sealed_ms", "max_records", "max_bytes", "stored", "bytes", "dropped",
             "first_drop_ms", "last_drop_ms")
    installed = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'kanban_fairness_*'"))
    complete = installed == _triggers(conn)
    # A seal intentionally removes triggers. Its pre-removal receipt, not current absence,
    # proves integrity. Older sealed journals without that receipt are unknown, never clean.
    seal_complete = None
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({SESSION})")}
    if "seal_triggers_complete" in columns:
        seal_complete = conn.execute(f"SELECT seal_triggers_complete FROM {SESSION} WHERE id = 1").fetchone()[0]
    last = conn.execute(f"SELECT MAX(seq) FROM {JOURNAL}").fetchone()[0]
    return {**dict(zip(names, row)), "last_seq": last or 0, "db": identity(conn),
            "triggers": sorted(installed), "triggers_complete": complete,
            "trigger_integrity": complete if row[2] is None else (
                seal_complete == 1 if seal_complete in (0, 1) else None)}


def arm(conn: sqlite3.Connection, capture_id: str, *, max_records: int = MAX_RECORDS,
        max_bytes: int = MAX_BYTES, replace: bool = False) -> dict:
    """Start a capture on this database: tables, opening snapshot and triggers in one transaction."""
    from hermes_cli.kanban_db_connect import write_txn

    if not capture_id or not all(c.isalnum() or c in "-_." for c in capture_id):
        raise JournalError("capture id must be letters, digits, '-', '_' or '.'")
    with write_txn(conn):
        if _has(conn, SESSION):
            if not replace:
                raise JournalError("a fairness capture already exists on this database; seal and clear it, "
                                   "or pass replace")
            _drop_triggers(conn)
            conn.execute(f"DROP TABLE IF EXISTS {JOURNAL}")
            conn.execute(f"DROP TABLE IF EXISTS {SESSION}")
        for ddl in _SCHEMA:
            conn.execute(ddl)
        conn.execute(f"INSERT INTO {SESSION} (id, capture_id, armed_ms, max_records, max_bytes) "
                     f"VALUES (1, ?, {_NOW_MS}, ?, ?)", (capture_id, max_records, max_bytes))
        present = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        fields = _fields_sql([c for c in TASK_FIELDS if c in present], "s")
        # The opening snapshot is not optional evidence: an armed session that could not hold it is a gap.
        # Rows over the byte bound on their own are gaps; the rest are admitted in id order while the running
        # UTF-8 byte total and the record count stay within bounds.
        conn.execute(
            f"INSERT INTO {JOURNAL} (kind, wall_ms, task_id, op, fields) SELECT 'opening', {_NOW_MS}, id, NULL, f "
            f"FROM (SELECT id, f, SUM(n) OVER (ORDER BY id) AS run, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM ("
            f"SELECT s.id AS id, {fields} AS f, length(CAST({fields} AS BLOB)) AS n FROM tasks AS s "
            "WHERE s.status NOT IN ('done', 'archived')) WHERE n <= ?) WHERE run <= ? AND rn <= ? ORDER BY id",
            (max_bytes, max_bytes, max_records))
        conn.execute(
            f"UPDATE {SESSION} SET stored = (SELECT COUNT(*) FROM {JOURNAL}), "
            f"bytes = (SELECT COALESCE(SUM(length(CAST(fields AS BLOB))), 0) FROM {JOURNAL}), "
            "dropped = MAX(0, (SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'archived')) - "
            f"(SELECT COUNT(*) FROM {JOURNAL})) WHERE id = 1")
        conn.execute(f"UPDATE {SESSION} SET first_drop_ms = armed_ms, last_drop_ms = armed_ms "
                     "WHERE id = 1 AND dropped > 0")
        for ddl in _triggers(conn).values():
            conn.execute(ddl)
    return _open_status(conn)


def _open_status(conn: sqlite3.Connection) -> dict:
    state = status(conn)
    if state is None:
        raise JournalError("no fairness capture on this database")
    return state


def seal(conn: sqlite3.Connection) -> dict:
    """Close the capture: stamp the end and remove the triggers. Rows stay for draining."""
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn):
        state = _open_status(conn)
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({SESSION})")}
        if "seal_triggers_complete" not in columns:
            conn.execute(f"ALTER TABLE {SESSION} ADD COLUMN seal_triggers_complete INTEGER")
        if state["sealed_ms"] is None:
            conn.execute(f"UPDATE {SESSION} SET seal_triggers_complete = ? WHERE id = 1",
                         (int(state["triggers_complete"]),))
        conn.execute(f"UPDATE {SESSION} SET sealed_ms = COALESCE(sealed_ms, {_NOW_MS}) WHERE id = 1")
        _drop_triggers(conn)
    return _open_status(conn)


def clear(conn: sqlite3.Connection) -> None:
    """Remove every trace of the capture from this database."""
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn):
        _drop_triggers(conn)
        conn.execute(f"DROP TABLE IF EXISTS {JOURNAL}")
        conn.execute(f"DROP TABLE IF EXISTS {SESSION}")


def rows(conn: sqlite3.Connection, after_seq: int = 0, limit: int = 5000) -> Iterator[tuple]:
    """``(seq, kind, wall_ms, task_id, op, fields_json)`` after ``after_seq``, in commit order."""
    yield from conn.execute(
        f"SELECT seq, kind, wall_ms, task_id, op, fields FROM {JOURNAL} WHERE seq > ? ORDER BY seq LIMIT ?",
        (after_seq, limit))
