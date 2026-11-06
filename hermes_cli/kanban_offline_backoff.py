"""Bound repeated offline escalations across probe deletion/repark. [daveey]"""
from __future__ import annotations

import json
import sqlite3


def recently_escalated(conn: sqlite3.Connection, task_id: str, cmd: str,
                       gateway: str, now: int, cooldown: int,
                       reasons: tuple[str, ...] = ("gateway_offline",)) -> bool:
    """Read durable (also pre-fix) wake receipts, not the disposable probe row.

    Age in the output continually increases and is not a new premise. A bounded
    persisted wall-clock deadline survives process restart and board replication.
    Only source-unavailable escalations (``reasons``: offline, or the owner's board
    scope unknown) are throttled; success and unexpected faults bypass it.
    """
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'wake_check_broken' "
        "AND created_at > ? AND created_at <= ?",
        (task_id, now - cooldown, now))
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get("reason") in reasons
                and payload.get("wake_check") == cmd and payload.get("gateway") == gateway):
            return True
    return False
