"""Capture-time administrative-home epochs; no production activation API yet.

A generation is replay protection, NOT authority to execute or release a hold.
Only native row capture stamps it. Repairs, synthetic parents and legacy pending
rows stay unknown: consulting today's mirror at send time would bless old writes.
The cooperating Board protocol must be deployed before installing any generation.
[daveey]
"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli.sqlite_util import add_column_if_missing


def install(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_home_generations1 "
                 "(task_id TEXT PRIMARY KEY, nonce TEXT NOT NULL)")
    add_column_if_missing(conn, "kanban_sync_outbox", "home_generations", "home_generations TEXT")
    # Keep this independent from removable sync capture triggers. Do not retrofit
    # a generation onto an extant pending op even if its source row now has one.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS home_generation1_outbox_immutable
        BEFORE UPDATE OF home_generations ON kanban_sync_outbox
        WHEN NEW.home_generations IS NOT OLD.home_generations
        BEGIN SELECT RAISE(ABORT, 'captured home generation is immutable'); END""")


def capture_sql(table: str, ref: str) -> str:
    """SQL evaluated within the native capture, including old delete identities."""
    if table == "tasks":
        ids = f"{ref}.id"
    elif table == "task_links":
        ids = f"{ref}.parent_id,{ref}.child_id"
    else:
        ids = f"{ref}.task_id"
    return ("(SELECT NULLIF(json_group_object(task_id,nonce),'{}') "
            f"FROM kanban_home_generations1 WHERE task_id IN ({ids}))")


def add_captured(row: dict, wire: dict) -> dict:
    """Never read the current task or generation store while building an envelope."""
    raw = row.get("home_generations")
    if raw is not None:
        value = json.loads(raw)
        if not isinstance(value, dict) or not value or any(not isinstance(k, str) or not isinstance(v, str)
                                                        for k, v in value.items()):
            raise ValueError("invalid captured home generation")
        wire = {**wire, "home_generations": value}
    return wire
