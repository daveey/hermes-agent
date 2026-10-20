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
# Waiting on a human. These don't keep the session busy, but the sidebar row
# shows how many there are next to the open count (``kanban_blocked``) and the
# row paints red. [daveey]
BLOCKED_STATUSES = ("blocked",)
# A ``blocked`` card only waits on a human for these kinds (NULL = generic human
# blocker). A ``transient``/``time`` block waits on a retry or a clock, so it
# counts as in-progress work instead, like ``scheduled``. [daveey]
HUMAN_BLOCK_KINDS = ("needs_input", "capability")

_CACHE_TTL_S = 3.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, int], dict[str, int]] | None = None


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


def _status_filter(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> tuple[str, tuple]:
    """SQL predicate on ``t`` for "in ``statuses``", splitting ``blocked`` by its kind:
    the human set (``BLOCKED_STATUSES``) keeps only human-kind blocks, the active set
    gains the ``transient``/``time`` ones. A board without ``block_kind`` is unchanged."""
    ph = ",".join("?" for _ in statuses)
    if not {r[1] for r in conn.execute("PRAGMA table_info(tasks)")} >= {"block_kind"}:
        return f"t.status IN ({ph})", statuses
    kinds = ",".join("?" for _ in HUMAN_BLOCK_KINDS)
    human = f"(t.block_kind IS NULL OR t.block_kind IN ({kinds}))"
    if statuses == BLOCKED_STATUSES:
        return f"(t.status = 'blocked' AND {human})", HUMAN_BLOCK_KINDS
    return f"(t.status IN ({ph}) OR (t.status = 'blocked' AND NOT {human}))", (*statuses, *HUMAN_BLOCK_KINDS)


def _read_counts(path: Path, statuses: tuple[str, ...] = ACTIVE_STATUSES) -> dict[str, int]:
    """Tasks per session in ``statuses``: tasks the session created, plus tasks
    it is a ``tui`` notify subscriber of (a session asked to shepherd someone
    else's card subscribes to it, and is just as "still working" until it
    lands). Each (session, task) pair counts once."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        match, params = _status_filter(conn, statuses)
        pairs: set[tuple[str, str]] = {
            (str(sid), str(tid)) for sid, tid in conn.execute(
                f"SELECT session_id, id FROM tasks t WHERE session_id IS NOT NULL "
                f"AND session_id != '' AND {match}",
                params,
            )
        }
        try:
            pairs.update(
                (str(sid), str(tid)) for sid, tid in conn.execute(
                    f"SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                    f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {match}",
                    params,
                )
            )
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    return counts


def _all_counts() -> tuple[dict[str, int], dict[str, int]]:
    """``(active, blocked)`` per-session task counts across every board, cached."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache[0]:
            return _cache[1], _cache[2]
    active: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for path in _board_db_paths():
        for statuses, into in ((ACTIVE_STATUSES, active), (BLOCKED_STATUSES, blocked)):
            try:
                for sid, n in _read_counts(path, statuses).items():
                    into[sid] = into.get(sid, 0) + n
            except sqlite3.Error as exc:  # an old schema without session_id, a locked file
                _log.debug("kanban activity read failed for %s: %s", path, exc)
    with _cache_lock:
        _cache = (now + _CACHE_TTL_S, active, blocked)
    return active, blocked


def active_task_counts() -> dict[str, int]:
    """``{session_id: active task count}`` across every kanban board."""
    return _all_counts()[0]


def blocked_task_counts() -> dict[str, int]:
    """``{session_id: blocked task count}`` across every kanban board."""
    return _all_counts()[1]


# What a session's task strip lists: the sidebar's open + blocked sets, plus
# ``review`` (also waiting on a human). [daveey]
LISTED_STATUSES = ACTIVE_STATUSES + BLOCKED_STATUSES + ("review",)
_TASK_FIELDS = ("id", "title", "status", "assignee", "session_id", "created_at", "started_at")


def _board_slug(path: Path) -> str:
    return path.parent.name if path.parent.parent.name == "boards" else "default"


def _read_session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """Tasks on one board that any of ``ids`` created or is a ``tui`` subscriber
    of, in ``statuses`` — the same pairs ``_read_counts`` counts."""
    sid_ph = ",".join("?" for _ in ids)
    st_ph = ",".join("?" for _ in statuses)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        select = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in _TASK_FIELDS)
        run_start = "NULL"
        if "current_run_id" in cols and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone():
            run_start = "(SELECT r.started_at FROM task_runs r WHERE r.id = t.current_run_id)"
        found: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
                f"SELECT {select}, {run_start} AS current_run_started_at FROM tasks t "
                f"WHERE t.session_id IN ({sid_ph}) AND t.status IN ({st_ph})", (*ids, *statuses)):
            found[row["id"]] = {**dict(row), "via": "created"}
        try:
            for row in conn.execute(
                    f"SELECT {select}, {run_start} AS current_run_started_at FROM kanban_notify_subs s "
                    f"JOIN tasks t ON t.id = s.task_id WHERE s.platform = 'tui' "
                    f"AND s.chat_id IN ({sid_ph}) AND t.status IN ({st_ph})", (*ids, *statuses)):
                found.setdefault(row["id"], {**dict(row), "via": "subscribed"})
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    board = _board_slug(path)
    return [{**t, "board": board} for t in found.values()]


def session_tasks(session_ids: Iterable[str], statuses: tuple[str, ...] = LISTED_STATUSES) -> list[dict[str, Any]]:
    """Every open/blocked/review task across all boards that the given session
    ids (one compression lineage) created or shepherd — exactly the cards the
    sidebar row's ``kanban_active``/``kanban_blocked`` counts cover. Never
    raises on a bad board; skips it."""
    ids = [s for s in dict.fromkeys(str(s) for s in session_ids if s)]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for path in _board_db_paths():
        try:
            out.extend(_read_session_tasks(path, ids, statuses))
        except sqlite3.Error as exc:
            _log.debug("kanban session tasks read failed for %s: %s", path, exc)
    return out


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


def annotate_kanban_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``kanban_active`` and ``kanban_blocked`` on each session row
    (summed over its whole compression lineage). Never raises: the list must
    render without them."""
    if not rows:
        return rows
    try:
        active, blocked = _all_counts()
    except Exception:
        _log.debug("kanban activity unavailable", exc_info=True)
        return rows
    for row in rows:
        ids = list(_row_ids(row))
        for key, counts in (("kanban_active", active), ("kanban_blocked", blocked)):
            n = sum(counts.get(sid, 0) for sid in ids)
            if n:
                row[key] = n
            else:
                row.pop(key, None)
    return rows


def _reset_cache_for_tests() -> None:
    global _cache
    with _cache_lock:
        _cache = None
