"""Append-only, host-local Board-private owner-release consumption cache.

Only the private authenticated GET populates it; never ordinary feed events.
Readers use SQL only (including actionability Rows) and confer historical proof,
NOT coverage, placement, spend or execution authority. HOME application is a
separate transaction and must not mistake a cached consumption for its result.
"""
from __future__ import annotations

import contextvars
import sqlite3

from hermes_cli import kanban_release_certificate as certificate
from hermes_cli.kanban_containment_functions import register as register_function

TABLE = "owner_release_consumption_receipts"
_PERMIT: contextvars.ContextVar[sqlite3.Connection | None] = contextvars.ContextVar("owner_release_cache_write", default=None)


def install(conn: sqlite3.Connection) -> None:
    register_function(conn, "kanban_owner_release_cache_write", 0, lambda: int(_PERMIT.get() is conn and conn.in_transaction))
    conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (consume_uid TEXT PRIMARY KEY, intent_uid TEXT NOT NULL UNIQUE, "
                 "board TEXT NOT NULL, task_id TEXT NOT NULL, digest TEXT NOT NULL, evidence TEXT NOT NULL)")
    abort = "BEGIN SELECT RAISE(ABORT,'owner release consumption receipt is immutable'); END"
    for op in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} "
                 f"WHEN NOT kanban_owner_release_cache_write() {abort}")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_replace BEFORE INSERT ON {TABLE} "
                 f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE consume_uid=NEW.consume_uid "
                 f"OR intent_uid=NEW.intent_uid OR rowid=NEW.rowid) {abort}")


def _board(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone():
        return None
    row = conn.execute("SELECT board_id FROM kanban_sync_state LIMIT 1").fetchone()
    return row[0] if row else None


def _store(conn: sqlite3.Connection, receipt: dict) -> None:
    """Private insertion after authenticated fetch+validation, never a sync importer."""
    from hermes_cli import kanban_db as kb

    body = receipt["intent"]
    raw = certificate.encode(receipt)
    install(conn)
    values = (receipt["consume"]["client_op_id"], body["intent_uid"], body["board"], body["task_id"], receipt["digest"], raw)
    with kb.write_txn(conn):
        if _board(conn) != body["board"]:
            raise ValueError("owner-release consumption belongs to a different board")
        prior = conn.execute(f"SELECT consume_uid,intent_uid,board,task_id,digest,evidence FROM {TABLE} "
                             "WHERE consume_uid=? OR intent_uid=?", values[:2]).fetchall()
        if prior:
            if len(prior) != 1 or tuple(prior[0]) != values:
                raise ValueError("owner-release consumption cache conflict")
            return
        token = _PERMIT.set(conn)
        try:
            conn.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?)", values)
        finally:
            _PERMIT.reset(token)


def certify(conn: sqlite3.Connection, hub, board: str, *, task: str, intent_uid: str) -> dict:
    """Fetch outside writers; immutable private endpoint is the certification boundary.

    Origin/HOME-only access is enforced by the authenticated Board endpoint,
    including its current gateway aliases. Do not reconstruct from public rows.
    """
    if conn.in_transaction:
        raise ValueError("owner-release certificate fetch cannot run in a transaction")
    if _board(conn) != board:
        raise ValueError("owner-release certificate belongs to a different board")
    receipt = certificate.validate(hub.owner_release_consumption(board, intent_uid), board=board, task=task, intent_uid=intent_uid)
    _store(conn, receipt)
    return receipt


def verified_consumption(conn, consume_uid: str) -> dict | None:
    """SQL-only historical proof. Never asserts HOME applied or any hold released."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    # Bound opaque evidence in SQL before materializing it, not after json.loads.
    row = conn.execute(f"SELECT consume_uid,intent_uid,board,task_id,digest,evidence FROM {TABLE} "
                       "WHERE consume_uid=? AND typeof(evidence)='text' AND length(CAST(evidence AS BLOB))<=?",
                       (consume_uid, certificate.MAX_BYTES)).fetchone()
    if row is None or row[2] != _board(conn):
        return None
    try:
        receipt = certificate.validate(certificate.parse(row[5]), board=row[2], task=row[3], intent_uid=row[1])
    except (TypeError, ValueError, UnicodeError):
        return None
    if row[0] != receipt["consume"]["client_op_id"] or row[4] != receipt["digest"]:
        return None
    return receipt
