"""Claim heartbeat (``kanban_db.heartbeat_claim``), moved out of ``kanban_db`` for its file-size cap.

``kanban_db`` re-exports it; callers keep using ``kanban_db.heartbeat_claim``.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional


def heartbeat_claim(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim; True if we still own it."""
    from hermes_cli import kanban_db as _kb

    expires = int(time.time()) + _kb._resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _kb._claimer_id()
    # [daveey] Only the claim's owner touches the Board lease. An ended worker's post-run turns still fire the
    # activity heartbeat, and heartbeat_expiry renews under the card's *current* run, which may be another worker's
    # (a refused renewal then logs a false "lease ... lost to" warning). Same predicate as the UPDATE below.
    if conn.execute("SELECT 1 FROM tasks WHERE id = ? AND status = 'running' AND claim_lock = ?",
                    (task_id, lock)).fetchone() is None:
        return False
    expires = _kb._sync.heartbeat_expiry(conn, task_id, expires)  # bounded by the lease on synced boards
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?", (expires, task_id, lock),
        )
        if cur.rowcount != 1:
            return False
        _kb._extend_run_claim(conn, task_id, expires)
        return True
