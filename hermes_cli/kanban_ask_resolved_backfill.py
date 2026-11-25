"""Backfill ``human_request.resolved_check_dropped_at`` for asks whose probe was dropped before the mark existed. [daveey]

Older cores appended ``BROKEN_EVENT`` / ``LOST_EVENT`` without marking the ask. ``kanban_ask_resolved.reconcile``
skips such a card forever (a terminal event follows its latest filing), so nothing tells origins or readers that
the probe stopped. This marks only a card that is still blocked, still names a resolved_check, has no probe row
and no mark, and whose *latest* ``blocked`` filing is followed by a drop event and no resolution. The mark takes
the drop event's own time and kind. The first mark atomically re-emits that kind so local origin cursors that
consumed the historical event get a notice; no comment is written, and the read pass takes no write txn.
A refiled ask has a newer ``blocked`` event, so an older drop never marks it. An ask whose probe another gateway
owns, or a worker-node copy of another gateway's card, is left to that owner.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

_DROPS = ("resolved_check_broken", "resolved_check_lost")
_RESOLVED = "ask_resolved"


def _latest_drop(conn: sqlite3.Connection, tid: str) -> Optional[tuple[int, int, str, int]]:
    """``(filed_id, drop_event_id, kind, drop_time)`` for the latest filing's unresolved drop, else None."""
    filed = conn.execute("SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                         "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    if filed is None:
        return None
    marks = ",".join("?" for _ in (*_DROPS, _RESOLVED))
    rows = conn.execute(f"SELECT id, kind, created_at FROM task_events WHERE task_id = ? AND id > ? "
                        f"AND kind IN ({marks}) ORDER BY id", (tid, filed[0], *_DROPS, _RESOLVED)).fetchall()
    drops = [r for r in rows if r[1] in _DROPS]
    if not drops or any(r[1] == _RESOLVED for r in rows):
        return None
    last = drops[-1]
    return int(filed[0]), int(last[0]), str(last[1]), int(last[2] or 0)


def _eligible(conn: sqlite3.Connection, tid: str) -> bool:
    from hermes_cli import kanban_ask_resolved as kar
    from hermes_cli import kanban_wake_check as kwc

    row = conn.execute("SELECT status, human_request FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None or row[0] != "blocked" or not kar.resolved_check(row[1])[0]:
        return False
    if (kar._request(row[1]) or {}).get("resolved_check_dropped_at"):
        return False
    return kwc.get_state(conn, tid) is None


def backfill(conn: sqlite3.Connection) -> list[str]:
    """Mark legacy dropped asks; returns the ids marked. Idempotent: a marked ask is never selected again."""
    from hermes_cli import kanban_ask_resolved as kar
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc
    from hermes_cli import kanban_home_probe as probe
    from hermes_cli.kanban_home_transfer import frozen

    try:
        ids = [r[0] for r in conn.execute(
            "SELECT t.id FROM tasks t LEFT JOIN task_wake_checks w ON w.task_id = t.id WHERE t.status = 'blocked' "
            "AND w.task_id IS NULL AND t.human_request LIKE '%\"resolved_check\"%' "
            "AND t.human_request NOT LIKE '%\"resolved_check_dropped_at\"%'").fetchall()]
    except sqlite3.OperationalError:
        return []
    own = kwc.own_gateway(conn)
    foreign = kwc.foreign_ids(conn, own)
    marked: list[str] = []
    for tid in ids:
        if frozen(conn, tid):
            continue
        drop = _latest_drop(conn, tid)
        if drop is None:
            continue
        with kb.write_txn(conn):
            if frozen(conn, tid) or _latest_drop(conn, tid) != drop or not _eligible(conn, tid):
                continue
            payload = conn.execute("SELECT payload FROM task_events WHERE id = ?", (drop[0],)).fetchone()
            host = probe.host(conn, tid, drop[0], kb._json_dict(payload[0]).get("probe_host") if payload else None)
            if host == probe.UNKNOWN or (host and own and host != own) or (tid in foreign and not host):
                continue
            kar.mark_dropped(conn, tid, drop[2], drop[3])
            # Sync remaps integer IDs but preserves UIDs and payloads verbatim. Older unsynced
            # schemas have no UID column; only those use the local reference.
            origin = conn.execute("SELECT * FROM task_events WHERE id = ?", (drop[1],)).fetchone()
            uid = origin["uid"] if "uid" in origin.keys() else None
            reference = {"origin_event_uid": uid} if uid else {"origin_event_id": drop[1]}
            kb._append_event(conn, tid, drop[2], {"backfilled": True, **reference})
            marked.append(tid)
    return marked
