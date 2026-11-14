"""The durable origin-work snapshot behind the remote-links projection (moved from kanban_session_activity, which
re-exports it as ``_remote_snapshot``: patch it there)."""

from __future__ import annotations

import json
from typing import Any


def _use_wal(conn) -> None:
    """Every process on the host reads and rewrites this file (a multi-MB row per scope). In a rollback journal a
    commit locks out every reader and any open reader fails the commit, so the remote-links rebuild's cold read
    died with ``database is locked`` (kanban-ec72354e6b). WAL lets reads proceed during a write. The shared helper
    keeps its policy: never on WAL-reset-vulnerable SQLite or WAL-hostile filesystems, never a live downgrade. [daveey]
    """
    from hermes_cli import kanban_lockguard
    from hermes_state_wal import apply_wal_with_fallback

    if apply_wal_with_fallback(conn, db_label="origin-work.db") == "wal":
        # Bound the -wal file: each save appends the whole scope row.
        conn.execute("PRAGMA journal_size_limit=8388608")
        kanban_lockguard.guard(conn)  # a file that only became WAL now gets its OFD lock guard


def remote_snapshot(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]] | None:
    """Durable last-known origin obligations, separate from the replaceable hub replica.

    A transaction replaces the snapshot atomically; failed writes propagate rather than
    claiming a durable association. This is a projection cache, not a second transport.
    """
    from hermes_cli import kanban_db
    from hermes_cli import kanban_session_activity as _ksa  # lazy: that module imports this one; tests spy its seams

    root = kanban_db.kanban_home()
    path = root / "origin-work.db"
    if cards is None:
        if not path.exists():
            return None
        scope = json.dumps(_ksa._origin_scope())
        conn = _ksa._open_ro(path)
    else:
        # The scope re-reads kanban_home(). A save racing an env swap (a test fixture moving HERMES_KANBAN_HOME)
        # filed one root's scope in another root's file: 20 pytest rows of ~1 MB in the hub's live file. [daveey]
        scope = json.dumps(_ksa._origin_scope())
        if kanban_db.kanban_home() != root:
            raise RuntimeError("kanban home moved while resolving the origin-work scope; not saving across roots")
        import hermes_state_guard

        hermes_state_guard.ensure_origin_work_test_isolation(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _ksa._kanban_lockguard.connect(path, timeout=0.5)
        try:
            _use_wal(conn)
        except BaseException:
            conn.close()
            raise
    try:
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
