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
database, cached by file change signals checked only on requests. No timer or
retained SQLite handle is needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

_KEY_PREFIX = "goal:"
_cache_lock = threading.Lock()
# Only immutable answers are retained; SQLite connections close after each scan.
_cache: dict[Path, tuple[tuple, frozenset[str]]] = {}
_directories: dict[Path, tuple[tuple, tuple[Path, ...]]] = {}
_MAX_CACHED_PATHS = 128


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return ()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _db_stamp(path: Path) -> tuple:
    return _stamp(path), _stamp(Path(str(path) + "-wal")), _stamp(Path(str(path) + "-journal"))


def _profile_candidates(directory: Path) -> tuple[Path, ...]:
    before = _stamp(directory)
    prior = _directories.get(directory)
    if prior is not None and prior[0] == before:
        return prior[1]
    _directories.pop(directory, None)
    # Include missing state.db candidates: creating one in an existing profile
    # need not change the profiles directory's mtime.
    paths = tuple(sorted(p / "state.db" for p in directory.iterdir())) if before else ()
    if before == _stamp(directory) and len(paths) <= _MAX_CACHED_PATHS:
        if len(_directories) >= _MAX_CACHED_PATHS:
            _directories.clear()
        _directories[directory] = before, paths
    return paths


def _state_db_paths() -> list[Path]:
    from hermes_constants import get_default_hermes_root, get_hermes_home

    roots = dict.fromkeys((Path(get_hermes_home()), Path(get_default_hermes_root())))
    paths: dict[Path, None] = {}
    for root in roots:
        paths[root / "state.db"] = None
        try:
            paths.update(dict.fromkeys(_profile_candidates(root / "profiles")))
        except OSError:
            _directories.pop(root / "profiles", None)
    return list(paths)


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
    with _cache_lock:
        paths = _state_db_paths()
        for removed in _cache.keys() - set(paths):
            del _cache[removed]
        ids: set[str] = set()
        for path in paths:
            try:
                before = _db_stamp(path)
                prior = _cache.get(path)
                if prior is not None and prior[0] == before:
                    ids.update(prior[1])
                    continue
                _cache.pop(path, None)
                if not before[0]:
                    continue
                active = frozenset(_read_active(path))
                ids.update(active)
                if before == _db_stamp(path) and len(_cache) < _MAX_CACHED_PATHS:
                    _cache[path] = before, active
            except (OSError, sqlite3.Error) as exc:
                # Never certify errors; the next request retries even without a write.
                _cache.pop(path, None)
                _log.debug("goal activity read failed for %s: %s", path, exc)
        return frozenset(ids)


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
    with _cache_lock:
        _cache.clear()
        _directories.clear()
