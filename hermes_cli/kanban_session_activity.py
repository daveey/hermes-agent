"""Which sessions still have kanban work in flight.

A session that hands work to ``kanban_create`` ends its own turn right away,
while the tasks it created keep running for hours in worker processes. The
sidebar painted such a session as "done" (green stripe) although its work was
still going on. Every kanban task records the session that created it
(``tasks.session_id``), so session-list endpoints stamp each row with how many
of its tasks are still pending or running (``kanban_active``); the desktop
treats a non-zero count like a running background job. [daveey]

Read-only and cheap: one indexed GROUP BY per board database, cached for a few
seconds because the sidebar polls.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

# Statuses that mean "work is still queued or running". ``blocked``/``review``
# wait on a human and ``done``/``archived`` are finished, so neither keeps the
# creating session busy.
ACTIVE_STATUSES = ("todo", "ready", "running", "scheduled")

_CACHE_TTL_S = 3.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, int]] | None = None


def _board_db_paths() -> list[Path]:
    from hermes_cli import kanban_db

    paths: list[Path] = []
    root = kanban_db.kanban_home()
    default = root / "kanban.db"
    if default.exists():
        paths.append(default)
    try:
        boards = kanban_db.boards_root()
        if boards.is_dir():
            paths.extend(p for p in sorted(boards.glob("*/kanban.db")) if p.is_file())
    except OSError:
        pass
    return paths


def _read_counts(path: Path) -> dict[str, int]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        rows = conn.execute(
            f"SELECT session_id, COUNT(*) FROM tasks WHERE session_id IS NOT NULL "
            f"AND session_id != '' AND status IN ({placeholders}) GROUP BY session_id",
            ACTIVE_STATUSES,
        ).fetchall()
    finally:
        conn.close()
    return {str(sid): int(n) for sid, n in rows}


def active_task_counts() -> dict[str, int]:
    """``{session_id: active task count}`` across every kanban board."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache[0]:
            return _cache[1]
    counts: dict[str, int] = {}
    for path in _board_db_paths():
        try:
            for sid, n in _read_counts(path).items():
                counts[sid] = counts.get(sid, 0) + n
        except sqlite3.Error as exc:  # an old schema without session_id, a locked file
            _log.debug("kanban activity read failed for %s: %s", path, exc)
    with _cache_lock:
        _cache = (now + _CACHE_TTL_S, counts)
    return counts


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


def annotate_kanban_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``kanban_active`` on each session row (summed over its whole
    compression lineage). Never raises: the list must render without it."""
    if not rows:
        return rows
    try:
        counts = active_task_counts()
    except Exception:
        _log.debug("kanban activity unavailable", exc_info=True)
        return rows
    for row in rows:
        n = sum(counts.get(sid, 0) for sid in _row_ids(row))
        if n:
            row["kanban_active"] = n
        else:
            row.pop("kanban_active", None)
    return rows


def _reset_cache_for_tests() -> None:
    global _cache
    with _cache_lock:
        _cache = None
