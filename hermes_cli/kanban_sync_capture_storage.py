"""Pending-custody provenance storage and conservative sticky taint. [daveey]

This substrate certifies NO captures: new and historical records default UNKNOWN.
Audited capture stamping, complete pre-I/O exposure history, old-writer drainage
and minimum-writer admission are separate mandatory activation prerequisites.
Columns disappear with ordinary pending custody, not a permanent operation ledger.
"""
from __future__ import annotations

import sqlite3

from hermes_cli.sqlite_util import add_column_if_missing

_COLUMNS = (
    ("capture_protocol", "TEXT"),
    ("original_op_id", "TEXT"),
    ("original_created_at", "INTEGER"),
    ("identity_state", "TEXT"),
)


def install(conn: sqlite3.Connection) -> None:
    """Add nullable columns, never retrofit originality from existing IDs/times.

    Keep these guards independent of removable sync capture triggers. Detaching
    sync must not erase an extant pending record's taint or immutable tuple.
    They do not make an old writer safe: delete/reinsert loses history and stays
    UNKNOWN, and no certification may start before the writer-version gate.
    """
    for table in ("kanban_sync_outbox", "kanban_sync_parent_envelopes"):
        for name, kind in _COLUMNS:
            add_column_if_missing(conn, table, name, f"{name} {kind}")
        unchanged = " AND ".join(f"NEW.{name} IS OLD.{name}" for name, _ in _COLUMNS[:3])
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS replay_capture1_{table}_immutable
            BEFORE UPDATE ON {table}
            WHEN NOT ({unchanged}) OR
                NOT (NEW.identity_state IS OLD.identity_state OR NEW.identity_state IS 'ambiguous')
            BEGIN SELECT RAISE(ABORT, 'capture provenance is immutable'); END""")
    # Frozen wire is a conservative exposure indicator, NOT a certified attempt
    # journal. A pre-I/O freeze followed by edits may over-taint, never certify.
    changed = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in (
        "table_name", "row_uid", "op", "fields", "base_version", "hlc", "wire_json", "status_origin"))
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS replay_capture1_outbox_taint
        AFTER UPDATE ON kanban_sync_outbox
        WHEN NEW.identity_state IS NOT 'ambiguous' AND
            (NEW.op_id IS NOT OLD.op_id OR NEW.created_at IS NOT OLD.created_at OR
                (OLD.wire_json IS NOT NULL AND ({changed})))
        BEGIN UPDATE kanban_sync_outbox SET identity_state='ambiguous' WHERE rowid=NEW.rowid; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS replay_capture1_parent_taint
        AFTER UPDATE ON kanban_sync_parent_envelopes
        WHEN NEW.identity_state IS NOT 'ambiguous' AND
            (NEW.task_id IS NOT OLD.task_id OR NEW.wire IS NOT OLD.wire)
        BEGIN UPDATE kanban_sync_parent_envelopes SET identity_state='ambiguous' WHERE rowid=NEW.rowid; END""")
