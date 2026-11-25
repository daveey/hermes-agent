"""Suspend ledger: which wall-clock spans this host's dispatcher spent asleep. [daveey]

A Mac lid-close stops every process, so a worker sends no heartbeat while it sleeps. The stale
checks that measure ``now - last_heartbeat_at`` (or ``now - started_at``) in wall-clock time then
read a long sleep as a wedged worker and SIGTERM it on the first pass after wake.

Each dispatcher pass calls :func:`record_dispatch_pass` before the reclaim sweeps. When the gap
since the previous pass exceeds :func:`gap_threshold_seconds` (``max(300, 3 x
kanban.dispatch_interval_seconds)``), the span ``[previous, now]`` is recorded as suspended. The
checks then use :func:`awake_seconds`, which subtracts recorded spans from a wall-clock window.

The ledger lives in ``kanban.db`` (two small tables created lazily here), so a gateway restart or a
one-shot ``hermes kanban dispatch`` sees the same history. Spans that ended more than 7 days ago are
pruned. A host that never sleeps (the hub) keeps an empty ledger, and :func:`awake_seconds` then
equals the wall-clock difference. A missing table (no pass recorded yet) also reads as empty.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

MIN_GAP_SECONDS = 300
GAP_INTERVAL_MULTIPLE = 3
RETENTION_SECONDS = 7 * 86400
DEFAULT_INTERVAL_SECONDS = 60

_DDL = (
    "CREATE TABLE IF NOT EXISTS kanban_suspend_ledger ("
    " start INTEGER NOT NULL, end INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kanban_dispatch_stamp ("
    " id INTEGER PRIMARY KEY CHECK (id = 1), at INTEGER NOT NULL)",
)


def gap_threshold_seconds(interval_seconds: Optional[float] = None) -> int:
    """A gap between passes longer than this counts as a suspend."""
    interval = interval_seconds if interval_seconds is not None else _configured_interval_seconds()
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_SECONDS
    return int(max(MIN_GAP_SECONDS, GAP_INTERVAL_MULTIPLE * max(interval, 0)))


def _configured_interval_seconds() -> float:
    try:
        from hermes_cli.config import load_config_readonly
        raw = ((load_config_readonly() or {}).get("kanban") or {}).get("dispatch_interval_seconds")
        return float(raw or DEFAULT_INTERVAL_SECONDS)
    except Exception:  # health: allow BLE001 -- an unreadable config must never stop a dispatch pass
        _log.debug("kanban: reading kanban.dispatch_interval_seconds failed; using %ss",
                   DEFAULT_INTERVAL_SECONDS, exc_info=True)
        return DEFAULT_INTERVAL_SECONDS


def record_dispatch_pass(
    conn: sqlite3.Connection, *, now: Optional[int] = None, interval_seconds: Optional[float] = None,
    dry_run: bool = False,
) -> Optional[tuple[int, int]]:
    """Stamp this pass; record ``[previous, now]`` as suspended when the gap is over the threshold.

    Returns the recorded span, else ``None``. Ordinary failures leave checks on wall-clock
    time; pass-wide storage cancellation propagates. A dry run (``dispatch_once(dry_run=True)``) is not a pass:
    it writes nothing, and its stamp would hide a real suspend gap from the next pass.
    """
    if dry_run:
        return None
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.kanban_dispatch_storage import DispatchWriteContended

    now = int(time.time()) if now is None else int(now)
    threshold = gap_threshold_seconds(interval_seconds)
    recorded = None
    try:
        with write_txn(conn):
            for ddl in _DDL:
                conn.execute(ddl)
            row = conn.execute("SELECT at FROM kanban_dispatch_stamp WHERE id = 1").fetchone()
            previous = int(row[0]) if row is not None else None
            if previous is not None and now - previous > threshold:
                conn.execute("INSERT INTO kanban_suspend_ledger (start, end) VALUES (?, ?)", (previous, now))
                recorded = (previous, now)
            conn.execute(
                "INSERT INTO kanban_dispatch_stamp (id, at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET at = excluded.at "
                "WHERE kanban_dispatch_stamp.at IS NOT excluded.at", (now,))
            conn.execute("DELETE FROM kanban_suspend_ledger WHERE end < ?", (now - RETENTION_SECONDS,))
    except DispatchWriteContended:
        raise  # The dispatcher owns this pass-wide skip, not a ledger failure.
    except (sqlite3.Error, RuntimeError):
        _log.warning("kanban: recording the dispatcher pass stamp failed", exc_info=True)
        return None
    if recorded is not None:
        _log.info("kanban: dispatcher was suspended for %ss (%s -> %s); stale checks skip it",
                  recorded[1] - recorded[0], recorded[0], recorded[1])
    return recorded


def suspended_intervals(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """Recorded suspend spans, oldest first; empty when none (or no table yet)."""
    try:
        rows = conn.execute("SELECT start, end FROM kanban_suspend_ledger ORDER BY start, end").fetchall()
    except sqlite3.OperationalError:
        return []
    return [(int(r[0]), int(r[1])) for r in rows]


def awake_seconds(conn: sqlite3.Connection, since: float, now: float) -> float:
    """``now - since`` minus the overlap of recorded suspends with ``[since, now]``."""
    total = now - since
    if total <= 0:
        return total
    try:
        rows = conn.execute(
            "SELECT start, end FROM kanban_suspend_ledger WHERE end > ? AND start < ? ORDER BY start",
            (since, now),
        ).fetchall()
    except sqlite3.OperationalError:
        return total
    asleep = 0.0
    cursor = since  # merge overlapping spans so none is subtracted twice
    for start, end in rows:
        lo, hi = max(float(start), cursor), min(float(end), now)
        if hi > lo:
            asleep += hi - lo
            cursor = hi
    return max(0.0, total - asleep)
