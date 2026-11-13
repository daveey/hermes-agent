"""Is this gateway's live board copy current enough to state a Board fact? Asked of its pulled replica. [daveey]

Defect t_01a1d390d19239dd (2026-11-12, MacBook): the dispatcher posted ``chat_wait_unclaimed`` ("ready with no
worker claiming it") for t_01a1d1292db9d3cb and t_01a1d12a594c8fd7 at 22:11 PST, 30 min after the Board completed
them. The live board's feed cursor (1650734) was ~62k rows behind its own pulled replica (``sync-shadow``, 1713384)
after a sync-loop stall, so the live rows still read ``ready``. Nothing compared the live copy with the replica.

The replica is the same Board feed applied into ``kanban/sync-shadow/<board>.db``; each successful pull stamps
``kanban_sync_state.pulled_at`` (``kanban_db_sync.mark_pulled``), the stamp ``kanban_chat_links`` already derives
replica staleness from. Two reads, both fail-open (no replica, an unsynced board or an unreadable file is no
evidence, and callers keep their old behaviour):

- :func:`lag_hold`: why every ready-wait notice waits now, when the live feed position trails the replica by more
  than :data:`MAX_FEED_LAG_ROWS` rows, or the replica's last pull is older than :data:`REPLICA_STALE_SECONDS` (a
  stalled sync loop stops both copies, so the row gap alone cannot see it).
- :func:`newer_rows`: the replica's rows of some cards where they carry a newer Board revision (``sync_version``)
  than the live rows.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

_log = logging.getLogger(__name__)

# Feed rows the live board may trail its replica before Board facts from it wait (~6 min at ~359 rows/min).
MAX_FEED_LAG_ROWS = 2000
# A replica not pulled for this long vouches for nothing: the sync loop (live apply included) is not running.
REPLICA_STALE_SECONDS = 600


def _live_state(conn) -> Optional[tuple[str, int]]:
    """``(board slug, feed cursor)`` of a live board attached to the Board service, else None."""
    try:
        row = conn.execute("SELECT board_id, cursor FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # never synced: no sync tables
        return None
    if row is None or not row[0] or str(row[0]).startswith("local:"):
        return None
    return str(row[0]), int(row[1] or 0)


def _replica(conn) -> Optional[tuple[Path, int]]:
    """``(replica path, live cursor)`` when this live board has a pulled replica on disk."""
    state = _live_state(conn)
    if state is None:
        return None
    from hermes_cli import kanban_sync_client as ksc
    path = ksc.shadow_path(state[0])
    return (path, state[1]) if path.exists() else None


def lag_hold(conn, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """``{"reason", "feed_lag_rows", "replica_pull_age_s"}`` while Board facts read from ``conn`` must wait, else
    None (no replica, an unreadable one, or a live copy within bounds)."""
    found = _replica(conn)
    if found is None:
        return None
    path, live_cursor = found
    now = time.time() if now is None else now
    from hermes_cli.kanban_session_activity import cached_ro  # lazy: a heavy module
    try:
        with cached_ro(path) as rconn:
            row = rconn.execute("SELECT cursor, pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        _log.debug("kanban replica freshness: %s unreadable (%s)", path, exc)
        return None
    if row is None:
        return None
    lag = int(row[0] or 0) - live_cursor
    age = None if row[1] is None else now - float(row[1])
    out = {"feed_lag_rows": lag, "replica_pull_age_s": None if age is None else int(age)}
    if lag > MAX_FEED_LAG_ROWS:
        return {**out, "reason": f"live feed {lag} rows behind its replica (bound {MAX_FEED_LAG_ROWS})"}
    if age is not None and age > REPLICA_STALE_SECONDS:
        return {**out, "reason": f"replica last pulled {int(age)} s ago (bound {REPLICA_STALE_SECONDS} s)"}
    return None


def newer_rows(conn, task_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``{task_id: replica row}`` for the cards whose replica row carries a strictly newer Board revision
    (``sync_version``) than the live row. A row is ``{"status", "ready_since", "sync_version", "result",
    "summary", "live_ready_since"}``. Cards with no replica row, or an older or equal one, are absent; so is
    everything when there is no readable replica."""
    ids = [str(t) for t in task_ids if t]
    found = _replica(conn) if ids else None
    if found is None:
        return {}
    path = found[0]
    marks = ",".join("?" * len(ids))
    try:
        live = {r[0]: (int(r[1] or 0), r[2]) for r in conn.execute(
            f"SELECT id, sync_version, ready_since FROM tasks WHERE id IN ({marks})", ids).fetchall()}
    except sqlite3.OperationalError:  # a board without the sync columns has no revision to compare
        return {}
    from hermes_cli.kanban_session_activity import cached_ro
    try:
        with cached_ro(path) as rconn:
            rows = rconn.execute(
                f"SELECT id, status, ready_since, sync_version, result FROM tasks WHERE id IN ({marks})",
                ids).fetchall()
            out = {r[0]: {"status": r[1], "ready_since": r[2], "sync_version": int(r[3] or 0), "result": r[4],
                          "live_ready_since": live.get(r[0], (0, None))[1]}
                   for r in rows if int(r[3] or 0) > live.get(r[0], (0, None))[0]}
            for tid, row in out.items():
                if row["status"] == "done":
                    s = rconn.execute(
                        "SELECT summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
                        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (tid,)).fetchone()
                    row["summary"] = s[0] if s else None
    except sqlite3.Error as exc:
        _log.debug("kanban replica freshness: %s unreadable (%s)", path, exc)
        return {}
    return out


def moved_past(row: Optional[dict[str, Any]]) -> Optional[str]:
    """Why a replica row (from :func:`newer_rows`) shows the live ``ready`` episode is over, or None: the Board
    has the card in another status, or ready again from a later episode (``ready_since``)."""
    if row is None:
        return None
    if row["status"] != "ready":
        return f"replica has it {row['status']} (revision {row['sync_version']})"
    if row.get("ready_since") and row.get("live_ready_since") and row["ready_since"] != row["live_ready_since"]:
        return f"replica has a later ready episode (revision {row['sync_version']})"
    return None
