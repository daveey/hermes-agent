"""Which sessions still own a running background process.

The sidebar paints a session green ("done") unless something it owns is still
working. The renderer only learned about background processes by polling a
session after one of its tool events streamed into the window, so a turn the
backend resumed after a restart, or a process registered by another Hermes
process, left the row green while ``gh pr checks --watch`` was still running.
Session-list endpoints now stamp each row with how many live background
processes its lineage owns (``background_active``). [daveey]

Sources: this process's registry (``tools.process_registry``) plus every
profile's ``processes.json`` checkpoint (shared by the Desktop backend, the
messaging gateway, CLI runs and kanban workers). Only processes whose host pid
is alive (and still the process that was spawned) count. Cached for a few
seconds because the sidebar polls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

_CACHE_TTL_S = 3.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, frozenset[str]]] | None = None

_SESSION_PREFIX = "session:"


def _owner_ids(item: dict[str, Any]) -> set[str]:
    """Session ids a process entry belongs to: its session key, the spawning
    conversation, and a ``session:<id>`` task/owner id."""
    ids: set[str] = set()
    for key in ("session_key", "parent_session_id", "owner_task_id", "task_id"):
        value = str(item.get(key) or "").strip()
        if value.startswith(_SESSION_PREFIX):
            value = value[len(_SESSION_PREFIX):]
        elif key in ("owner_task_id", "task_id") and value in ("", "default"):
            continue
        if value:
            ids.add(value)
    return ids


def _checkpoint_paths() -> list[Path]:
    paths: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        paths.append(home / "processes.json")
        profiles = home / "profiles"
        if profiles.is_dir():
            paths.extend(sorted(profiles.glob("*/processes.json")))
    except Exception:
        _log.debug("process checkpoint paths unavailable", exc_info=True)
    try:
        from tools.process_registry import _checkpoint_path

        current = _checkpoint_path()
        if current not in paths:
            paths.append(current)
    except Exception:
        pass
    return [p for p in paths if p.is_file()]


def _live(pid: Any, start: Any) -> bool:
    from tools.process_registry import ProcessRegistry

    return isinstance(pid, int) and pid > 0 and ProcessRegistry._host_pid_is_ours(pid, start)


def _collect() -> dict[str, frozenset[str]]:
    pairs: set[tuple[str, str]] = set()
    try:
        from tools.process_registry import process_registry

        with process_registry._lock:
            running = [s for s in process_registry._running.values() if not s.exited]
        for s in running:
            if s.pid_scope == "host" and not _live(s.pid, s.host_start_time):
                continue
            item = {"session_key": s.session_key, "parent_session_id": s.parent_session_id,
                    "owner_task_id": s.owner_task_id, "task_id": s.task_id}
            pairs.update((sid, s.id) for sid in _owner_ids(item))
    except Exception:
        _log.debug("in-process registry unavailable", exc_info=True)
    for path in _checkpoint_paths():
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for item in entries if isinstance(entries, list) else ():
            if not isinstance(item, dict) or item.get("pid_scope", "host") != "host":
                continue
            if not _live(item.get("pid"), item.get("host_start_time")):
                continue
            proc_id = str(item.get("session_id") or item.get("pid"))
            pairs.update((sid, proc_id) for sid in _owner_ids(item))
    owned: dict[str, set[str]] = {}
    for sid, proc in pairs:
        owned.setdefault(sid, set()).add(proc)
    return {sid: frozenset(procs) for sid, procs in owned.items()}


def _owned_processes() -> dict[str, frozenset[str]]:
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache[0]:
            return _cache[1]
    owned = _collect()
    with _cache_lock:
        _cache = (now + _CACHE_TTL_S, owned)
    return owned


def background_process_counts() -> dict[str, int]:
    """``{session_id: live background process count}``, cached briefly."""
    return {sid: len(procs) for sid, procs in _owned_processes().items()}


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


def annotate_background_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``background_active`` on each session row (summed over its
    compression lineage; a process counts once per row). Never raises."""
    if not rows:
        return rows
    try:
        owned = _owned_processes()
    except Exception:
        _log.debug("background activity unavailable", exc_info=True)
        return rows
    for row in rows:
        n = len(set().union(*(owned.get(sid, ()) for sid in _row_ids(row))))
        if n:
            row["background_active"] = n
        else:
            row.pop("background_active", None)
    return rows


def _reset_cache_for_tests() -> None:
    global _cache
    with _cache_lock:
        _cache = None
