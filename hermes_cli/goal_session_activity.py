"""Which sessions still have a standing goal in flight.

A ``/goal`` session ends each turn normally, then the goal loop judges it and
either kicks the next turn or parks it (``verdict=wait``) until a timer fires.
Between turns the sidebar painted such a session as "done" (green stripe)
although the goal would resume on its own. The goal lives in the session's
``state_meta`` row (``goal:<session_id>``, see ``hermes_cli.goals``), so
session-list endpoints stamp each row whose goal is still ``active``
(``goal_active``); the desktop treats it like a running background job.
Paused, done and cleared goals don't count: nothing resumes them unasked.
[daveey]

Read-only and cheap: one primary-key range scan of ``state_meta`` per profile
database, cached for a couple of seconds because the sidebar polls.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

_KEY_PREFIX = "goal:"
_CACHE_TTL_S = 2.0
_cache_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] | None = None


def _state_db_paths() -> list[Path]:
    from hermes_constants import get_default_hermes_root, get_hermes_home

    paths: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p.resolve()) if p.exists() else ""
        if key and key not in seen:
            seen.add(key)
            paths.append(p)

    roots = [get_hermes_home()]
    try:
        roots.append(get_default_hermes_root())
    except Exception:  # pragma: no cover - defensive
        pass
    for root in roots:
        add(Path(root) / "state.db")
        prof = Path(root) / "profiles"
        try:
            if prof.is_dir():
                for p in sorted(prof.glob("*/state.db")):
                    add(p)
        except OSError:
            pass
    return paths


def _read_active(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        # Range scan on the PRIMARY KEY (LIKE can't use it under NOCASE rules).
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key >= ? AND key < ?",
            (_KEY_PREFIX, _KEY_PREFIX[:-1] + chr(ord(_KEY_PREFIX[-1]) + 1)),
        ).fetchall()
    finally:
        conn.close()
    out: set[str] = set()
    for key, raw in rows:
        try:
            data = json.loads(raw or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("status") == "active" and str(data.get("goal") or "").strip():
            out.add(str(key)[len(_KEY_PREFIX):])
    return out


def active_goal_sessions() -> frozenset[str]:
    """Session ids (across every profile) whose standing goal is still active."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache[0]:
            return _cache[1]
    ids: set[str] = set()
    for path in _state_db_paths():
        try:
            ids |= _read_active(path)
        except sqlite3.Error as exc:  # no state_meta yet, a locked file
            _log.debug("goal activity read failed for %s: %s", path, exc)
    result = frozenset(ids)
    with _cache_lock:
        _cache = (now + _CACHE_TTL_S, result)
    return result


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


def annotate_goal_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``goal_active: True`` on rows whose lineage carries an active goal.
    Never raises: the list must render without it."""
    if not rows:
        return rows
    try:
        active = active_goal_sessions()
    except Exception:
        _log.debug("goal activity unavailable", exc_info=True)
        return rows
    for row in rows:
        if active and any(sid in active for sid in _row_ids(row)):
            row["goal_active"] = True
        else:
            row.pop("goal_active", None)
    return rows


def _reset_cache_for_tests() -> None:
    global _cache
    with _cache_lock:
        _cache = None
