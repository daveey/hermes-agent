"""Transactional invalidation for the sync client's repair scans, not dispatcher bookkeeping."""

from __future__ import annotations

import sqlite3

# These are the live inputs of partition links, remote parents, divergence, missing
# children and placeholder retirement. Keep independent of the capture triggers:
# repairing writes made while capture is absent is the reason these scans exist.
TABLES = (
    "tasks", "task_runs", "task_links", "task_chat_links",
    "kanban_sync_outbox", "kanban_sync_tombstones", "kanban_sync_remote_parents", "kanban_sync_omissions",
)
REVISION_TABLE = "kanban_repair_revision"


def ensure_revision(conn: sqlite3.Connection) -> None:
    """Install/repair the counter and its triggers atomically on an existing synced board.

    No write transaction on the normal path. Triggers use a separate prefix so
    capture disable/reinstall cannot create an invisible repair-input change.
    Schema changes also enter the reader's key, including a removed trigger.
    """
    expected = {
        f"kanban_repair_{table}_{op.lower()}":
        f"CREATE TRIGGER kanban_repair_{table}_{op.lower()} AFTER {op} ON {table} "
        f"BEGIN UPDATE {REVISION_TABLE} SET revision = revision + 1 WHERE id = 1; END"
        for table in TABLES for op in ("INSERT", "UPDATE", "DELETE")
    }
    found = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'kanban_repair_*'"))
    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                             (REVISION_TABLE,)).fetchone()
    if all(found.get(name) == sql for name, sql in expected.items()) and has_table and conn.execute(
        f"SELECT 1 FROM {REVISION_TABLE} WHERE id = 1"
    ).fetchone():
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {REVISION_TABLE} "
                     "(id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL)")
        conn.execute(f"INSERT OR IGNORE INTO {REVISION_TABLE} VALUES (1, 0)")
        # A capture gap or replaced definition must invalidate even if no data
        # write was observed while the trigger was absent.
        conn.execute(f"UPDATE {REVISION_TABLE} SET revision = revision + 1 WHERE id = 1")
        for name, sql in expected.items():
            if found.get(name) != sql:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(sql)
