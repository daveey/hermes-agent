"""The durable origin-work snapshot behind the remote-links projection (moved from kanban_session_activity, which
re-exports it as ``_remote_snapshot``: patch it there)."""

from __future__ import annotations

import json
from typing import Any


def remote_snapshot(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]] | None:
    """Durable last-known origin obligations, separate from the replaceable hub replica.

    A transaction replaces the snapshot atomically; failed writes propagate rather than
    claiming a durable association. This is a projection cache, not a second transport.
    """
    from hermes_cli import kanban_db
    from hermes_cli import kanban_session_activity as _ksa  # lazy: that module imports this one; tests spy its seams

    path = kanban_db.kanban_home() / "origin-work.db"
    if cards is None:
        if not path.exists():
            return None
        conn = _ksa._open_ro(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _ksa._kanban_lockguard.connect(path, timeout=0.5)
    try:
        scope = json.dumps(_ksa._origin_scope())
        if cards is not None:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS scoped_snapshot (scope TEXT PRIMARY KEY, data TEXT NOT NULL)")
                conn.execute("INSERT OR REPLACE INTO scoped_snapshot VALUES (?, ?)", (scope, json.dumps(cards)))
            return cards
        # Historical snapshots omitted the origin profile. Never adopt or erase
        # their obligations merely because a new scoped projection is available.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshot'").fetchone():
            old = conn.execute("SELECT data FROM snapshot WHERE id=1").fetchone()
            if old is None or json.loads(old[0]) != []:
                raise ValueError("unqualified origin-work snapshot requires explicit reconciliation")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_snapshot'").fetchone():
            return None
        row = conn.execute("SELECT data FROM scoped_snapshot WHERE scope=?", (scope,)).fetchone()
        if row is None:
            return None
        saved = json.loads(row[0])
        if not isinstance(saved, list) or any(not isinstance(c, dict) or not c.get("session") or not c.get("id") for c in saved):
            raise ValueError("invalid origin-work snapshot")
        return saved
    finally:
        conn.close()
