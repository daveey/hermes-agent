"""Capture-time administrative-home epochs; no production activation API yet.

A generation is replay protection, NOT authority to execute or release a hold.
Only native row capture stamps it. Repairs, synthetic parents and legacy pending
rows stay unknown: consulting today's mirror at send time would bless old writes.
The cooperating Board protocol must be deployed before installing any generation.
[daveey]
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sqlite3

from hermes_cli.sqlite_util import add_column_if_missing


def install(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_home_generations1 "
                 "(task_id TEXT PRIMARY KEY, nonce TEXT NOT NULL)")
    add_column_if_missing(conn, "kanban_home_generations1", "version", "version INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "kanban_home_generations1", "pending", "pending INTEGER NOT NULL DEFAULT 1")
    from hermes_cli import kanban_home_revision as revision
    revision.install(conn, ('kanban_home_generations1',))
    add_column_if_missing(conn, "kanban_sync_outbox", "home_generations", "home_generations TEXT")
    # Pending custody fences the WHOLE task row like prepared custody does, including
    # local-only claim bookkeeping (worker_pid, claim_lock) that the synced-column
    # capture triggers never see. Board apply (applying=1) is exempt: it is how the
    # pending mirror itself is fed. Installed with sync schema on every connect, so
    # upgraded/reconnected stores gain it; stores without the sync schema have no
    # pending mirror at all. Cooperating clients only; DDL is out of scope.
    from hermes_cli.kanban_db_sync import _NOT_APPLYING
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS home_pending1_tasks_update
        BEFORE UPDATE ON tasks WHEN {_NOT_APPLYING} AND EXISTS(SELECT 1 FROM kanban_home_generations1
            WHERE task_id IN (OLD.id, NEW.id) AND pending != 0)
        BEGIN SELECT RAISE(ABORT, 'home transfer pending'); END""")
    # Keep this independent from removable sync capture triggers. Do not retrofit
    # a generation onto an extant pending op even if its source row now has one.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS home_generation1_outbox_immutable
        BEFORE UPDATE OF home_generations ON kanban_sync_outbox
        WHEN NEW.home_generations IS NOT OLD.home_generations
        BEGIN SELECT RAISE(ABORT, 'captured home generation is immutable'); END""")


def mirror_feed(conn: sqlite3.Connection, task: str, fields: Mapping, version: int) -> None:
    """Only the authenticated Board apply path calls this; a field is not authority.

    Keep the monotonic mirror after task eviction. Missing legacy metadata never
    clears an epoch. Native capture reads it in the same writer transaction as
    the applied row; previously captured intents remain byte-identical.
    """
    nonce = fields.get("home_generation")
    if nonce is None:
        return
    if (not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", nonce)
            or type(version) is not int or version < 1):
        raise ValueError("invalid feed home generation")
    old = conn.execute("SELECT nonce,version,pending FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()
    # Absence is unknown/pending, never an implicit source-STOP certificate.
    flag = fields.get("home_transfer_pending", nonce)
    if flag is not None and flag != nonce:
        raise ValueError("invalid feed home generation pending scope")
    pending = int(flag is not None)
    if old and (version < old[1] or (version == old[1] and (nonce != old[0] or pending != old[2]))):
        raise ValueError("feed home generation regressed")
    conn.execute("INSERT INTO kanban_home_generations1(task_id,nonce,version,pending) VALUES (?,?,?,?) "
                 "ON CONFLICT(task_id) DO UPDATE SET nonce=excluded.nonce,version=excluded.version,pending=excluded.pending",
                 (task, nonce, version, pending))


def pending(conn: sqlite3.Connection, task: str) -> bool:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_home_generations1'").fetchone():
        return False
    columns = {r[1] for r in conn.execute("PRAGMA table_info(kanban_home_generations1)")}
    if "pending" not in columns:
        return bool(conn.execute("SELECT 1 FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone())
    row = conn.execute("SELECT pending FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()
    return bool(row and row[0] != 0)


def mirror_change(conn: sqlite3.Connection, change) -> None:
    """Include own echoes, which intentionally skip business-row reapplication."""
    if change.table != "tasks" or change.op != "upsert":
        return
    if conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name='tasks' AND row_uid=?", (change.uid,)).fetchone():
        return
    row = conn.execute("SELECT sync_version FROM tasks WHERE id=?", (change.uid,)).fetchone()
    if row and row[0] is not None and change.version < row[0]:
        return
    mirror_feed(conn, change.uid, change.fields, change.version)


def capture_sql(table: str, ref: str) -> str:
    """SQL evaluated within the native capture, including old delete identities."""
    if table == "tasks":
        ids = f"{ref}.id"
    elif table == "task_links":
        ids = f"{ref}.parent_id,{ref}.child_id"
    else:
        ids = f"{ref}.task_id"
    return ("(SELECT CASE WHEN EXISTS(SELECT 1 FROM kanban_home_generations1 "
            f"WHERE task_id IN ({ids}) AND pending != 0) "
            "THEN RAISE(ABORT,'home transfer pending') ELSE NULLIF(json_group_object(task_id,nonce),'{}') END "
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
