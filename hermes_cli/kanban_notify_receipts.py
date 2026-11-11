"""Durable, per-subscription failure delivery receipts. [daveey]

Ping and wake are independent obligations. Record only successful delivery, not
claiming; failed wakes must remain retryable even when the ping already landed.
As with the existing ping cursor, an ambiguous transport ACK is not exactly-once
transport: adapters must supply that guarantee themselves.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_notify_receipts (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        platform TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        lane TEXT NOT NULL, run_uid TEXT NOT NULL,
        PRIMARY KEY (task_id, platform, chat_id, thread_id, lane, run_uid)
    )""")


def notify_failure_receipts(conn, *, task_id, platform, chat_id, thread_id=None, lane, run_uids=()):
    """Checkpoint successful run deliveries, and return the lane's delivered keys."""
    if lane not in {"ping", "wake"}:
        raise ValueError("unknown notification receipt lane")
    key = (task_id, platform.lower(), chat_id, thread_id or "", lane)
    with kb.write_txn(conn):
        _ensure(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO kanban_notify_receipts VALUES (?, ?, ?, ?, ?, ?)",
            [(*key, uid) for uid in run_uids],
        )
        return {row[0] for row in conn.execute(
            "SELECT run_uid FROM kanban_notify_receipts WHERE task_id=? AND platform=? "
            "AND chat_id=? AND thread_id=? AND lane=?", key,
        )}
