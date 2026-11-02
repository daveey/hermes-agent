"""The hub replica's content signature behind the annotation's fingerprint (moved from kanban_session_activity)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_HUB_SIGNATURE_SQL = ("SELECT coll, COUNT(*), MAX(seq), TOTAL(seq) FROM replica WHERE coll IN ('tasks', 'gateway') "
                      "GROUP BY coll")
_hub_signature_memo: tuple | None = None  # ((path, stat key), signature)


def _hub_content_signature(hub: Path) -> tuple:
    """What the remote-links projection reads from the hub replica (``tasks`` and ``gateway`` rows, ``meta``
    gateway), not the file's stat: hub-sync rewrites ~10k ``sessions`` rows continuously, so a stat-keyed
    fingerprint changed on nearly every read and every caller re-projected 2.4k cards (~19% of a Desktop
    backend's GIL time, 2026-10-27). ``seq`` is the replica's global write sequence, so COUNT + MAX + TOTAL of it
    moves on any insert, update or delete of those rows. The query (~10 ms) re-runs only when the file's stat
    moves; an unreadable replica falls back to the stat itself. [daveey]"""
    global _hub_signature_memo
    from hermes_cli import kanban_session_activity as _ksa  # lazy: that module imports this one; tests spy _open_ro
    stat_key = []
    for path in (hub, Path(str(hub) + "-wal")):
        try:
            st = path.stat()
            stat_key.append((st.st_ino, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            stat_key.append(None)
    memo_key = (str(hub), tuple(stat_key))
    memo = _hub_signature_memo
    if memo is not None and memo[0] == memo_key:
        return memo[1]
    try:
        conn = _ksa._open_ro(hub)
        try:
            rows = tuple(tuple(r) for r in conn.execute(_HUB_SIGNATURE_SQL).fetchall())
            meta = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
        finally:
            conn.close()
        signature = (str(hub), stat_key[0][0] if stat_key[0] else None, rows, meta[0] if meta else None)
    except (sqlite3.Error, OSError):
        signature = memo_key
    _hub_signature_memo = (memo_key, signature)
    return signature


def reset() -> None:
    global _hub_signature_memo
    _hub_signature_memo = None
