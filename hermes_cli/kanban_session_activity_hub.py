"""The hub replica's content signature behind the annotation's fingerprint (moved from kanban_session_activity)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli.kanban_activity_revision import file_stat

_HUB_SIGNATURE_SQL = ("SELECT coll, COUNT(*), MAX(seq), TOTAL(seq) FROM replica WHERE coll IN ('tasks', 'gateway') "
                      "GROUP BY coll")
_hub_signature_memo: tuple | None = None


def _hub_content_signature(hub: Path) -> tuple:
    """Ignore session-only writes, but never mistake an equal stat for no commit.

    SQLite's data_version on a kept read-only connection detects commits even on
    coarse-mtime filesystems. Include the connection itself: version numbers from
    different connections are not comparable. Recompute the content projection
    only after a commit, preserving the cheap unchanged-poll path. [daveey]
    """
    global _hub_signature_memo
    from hermes_cli import kanban_session_activity as _ksa

    stat = file_stat(hub)
    try:
        with _ksa.cached_ro(hub, busy_budget=0) as conn:
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            memo_key = (str(hub), stat, conn, version)
            memo = _hub_signature_memo
            if memo is not None and memo[0] == memo_key:
                return memo[1]
            conn.execute("BEGIN")
            try:
                try:
                    rows = tuple(tuple(r) for r in conn.execute(_HUB_SIGNATURE_SQL).fetchall())
                    meta = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
                    signature = (str(hub), stat[0][:2] if stat[0] else None, rows, meta[0] if meta else None)
                except sqlite3.OperationalError:
                    # Old replicas lack seq. Every commit must invalidate them.
                    signature = memo_key
            finally:
                conn.execute("ROLLBACK")
            _hub_signature_memo = (memo_key, signature)
            return signature
    except (sqlite3.Error, OSError):
        # An unreadable replica is unknown, not evidence that cached rows are current.
        return (str(hub), stat, object())


def reset() -> None:
    global _hub_signature_memo
    _hub_signature_memo = None
