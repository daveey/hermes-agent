"""Local-only portable identities and reconnect registration.

No sync state is created or detached. A previously synced file refuses local
identity bootstrap even when its ambient configuration has sync disabled.
"""
from __future__ import annotations

import sqlite3


def bootstrap(conn: sqlite3.Connection) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_containment_lifecycle import _local
    from hermes_cli.kanban_db_sync import uuid7
    with kb.write_txn(conn):
        _local(conn)
        for table in ("task_runs", "task_events"):
            if "uid" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
            for (rid,) in conn.execute(f"SELECT id FROM {table} WHERE uid IS NULL").fetchall():
                conn.execute(f"UPDATE {table} SET uid=? WHERE id=? AND uid IS NULL", (uuid7(), rid))


def verifier(conn: sqlite3.Connection, receipt, stop) -> bool:
    synced = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone()
    if synced and conn.execute("SELECT 1 FROM kanban_sync_state LIMIT 1").fetchone():
        from hermes_cli.kanban_containment_replica import verified
        return verified(conn, receipt, stop)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_attestations'").fetchone():
        return False
    from hermes_cli.kanban_containment_lifecycle import verified_release
    return verified_release(conn, receipt, stop)


def register(conn: sqlite3.Connection) -> None:
    """Read/UDF registration only; no transactions, DDL, or network on connect."""
    from hermes_cli.kanban_containment_guard import register as register_guard
    register_guard(conn, verify_release=lambda receipt, stop: verifier(conn, receipt, stop))


def held(conn: sqlite3.Connection, task_id: str) -> bool:
    from hermes_cli.kanban_containment_guard import pending
    return pending(conn, task_id, verify_release=lambda receipt, stop: verifier(conn, receipt, stop))


def drop_held(conn: sqlite3.Connection, rows: list) -> list:
    """Benign dispatch/maintenance exclusion; never a lifecycle transition."""
    return [row for row in rows if not held(conn, row["id"])]


def refusal(conn: sqlite3.Connection, task_id: str) -> str | None:
    from hermes_cli.kanban_sync_quarantine import held as quarantined, refusal as quarantine_refusal
    if quarantined(conn, task_id):  # a host-local hold: resume-contained has no episode to release  [daveey]
        return quarantine_refusal(conn, task_id)
    return "containment_pending: scoped resume-contained required" if held(conn, task_id) else None


def released_history_delete(conn: sqlite3.Connection, task_id: str):
    """Delete custody: only for a task whose containment is verifiably released."""
    from hermes_cli.kanban_containment_guard import released_history_delete as scoped
    return scoped(conn, task_id, verify_release=lambda receipt, stop: verifier(conn, receipt, stop))
