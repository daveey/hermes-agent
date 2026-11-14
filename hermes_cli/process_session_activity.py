"""Which sessions still own a running background process.

The sidebar paints a session green ("done") unless something it owns is still
working. The renderer only learned about background processes by polling a
session after one of its tool events streamed into the window, so a turn the
backend resumed after a restart, or a process registered by another Hermes
process, left the row green while ``gh pr checks --watch`` was still running.
Session-list endpoints now stamp each row with how many live background
processes its lineage owns (``background_active``) and what they are
(``background_processes``: id, command, start time), so the row can show WHY it
is not done and stop a process from the sidebar. [daveey]

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

from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS
from hermes_cli.turn_process_execution_observations import PROCESS_SOURCES, process, profile_for_state_path, replace

_log = logging.getLogger(__name__)

_CACHE_TTL_S = 3.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, frozenset[str]]] | None = None
_details: dict[str, dict[str, Any]] = {}
# Evidence behind _details for execution observations; never copied into row detail lists.
_evidence: dict[str, dict[str, Any]] = {}

_SESSION_PREFIX = "session:"
# Per-row detail list caps: the sidebar tooltip shows a handful, and the list
# endpoint must stay small.
_MAX_ROW_PROCESSES = 8
_MAX_COMMAND_CHARS = 160


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


def _detail(proc_id: str, command: Any, started_at: Any, pid: Any) -> dict[str, Any]:
    text = " ".join(str(command or "").split())
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, code_file=True)
    except Exception:
        pass
    if len(text) > _MAX_COMMAND_CHARS:
        text = text[: _MAX_COMMAND_CHARS - 1] + "\u2026"
    out: dict[str, Any] = {"id": proc_id, "command": text or "background process"}
    if isinstance(started_at, (int, float)) and started_at > 0:
        out["started_at"] = float(started_at)
    if isinstance(pid, int) and pid > 0:
        out["pid"] = pid
    return out


def _registry_profile(session_key: str) -> str | None:
    """A named multiplex route qualifies its owner; legacy ``agent:main`` does not."""
    parts = session_key.split(":")
    if len(parts) < 3 or parts[0] != "agent" or parts[1] == "main":
        return None
    return "main" if parts[1] == "main~" else parts[1]


def _collect() -> dict[str, frozenset[str]]:
    global _details, _evidence
    details: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
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
            details[s.id] = _detail(s.id, s.command, s.started_at, s.pid)
            evidence[s.id] = {"id": s.id, "pid": s.pid, "host_start_time": s.host_start_time,
                              "profile": _registry_profile(s.session_key),
                              "pid_scope": s.pid_scope, "source": "process.registry", "observed_at": time.time(),
                              "owners": frozenset(_owner_ids(item))}
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
            if proc_id not in evidence:  # a pid-only checkpoint has no id: its observation stays unknown
                evidence[proc_id] = {"id": item.get("session_id") or None, "pid": item.get("pid"),
                                     "host_start_time": item.get("host_start_time"),
                                     "pid_scope": "host", "profile": profile_for_state_path(path), "source": "process.checkpoint",
                                     "observed_at": time.time(), "owners": frozenset(_owner_ids(item))}
            pairs.update((sid, proc_id) for sid in _owner_ids(item))
            details.setdefault(proc_id, _detail(proc_id, item.get("command"), item.get("started_at"),
                                                item.get("pid")))
    owned: dict[str, set[str]] = {}
    for sid, proc in pairs:
        owned.setdefault(sid, set()).add(proc)
    _details = details
    _evidence = evidence
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
    compression lineage; a process counts once per row) and
    ``background_processes`` (oldest first: id, command, started_at, pid).
    Never raises."""
    if not rows:
        return rows
    try:
        owned = _owned_processes()
    except Exception:
        _log.debug("background activity unavailable", exc_info=True)
        return rows
    details = _details
    evidence = _evidence
    now = time.time()
    try:
        from hermes_cli.kanban_execution_observations import local_scope
        gateway, local_profile = local_scope()
    except ADAPTER_ERRORS:
        _log.debug("process gateway identity unavailable", exc_info=True)
        gateway = local_profile = None
    for row in rows:
        ids = set(_row_ids(row))
        procs = set().union(*(owned.get(sid, ()) for sid in ids))
        # A row supplies missing registry scope, never overrides recorded foreign ownership.
        if row.get("profile"):
            procs = {p for p in procs if not evidence.get(p, {}).get("profile")
                     or evidence[p]["profile"] == row["profile"]}
        try:
            observations = []
            for proc_id in sorted(procs):
                entry = evidence.get(proc_id)
                if entry is None:
                    continue
                owners = sorted(entry["owners"] & ids)
                observations.append(process(entry, gateway=gateway, profile=entry["profile"] or row.get("profile") or local_profile,
                                            session=owners[0] if owners else None, now=now))
            replace(row, PROCESS_SOURCES, observations)
        except ADAPTER_ERRORS:
            _log.debug("process execution observation unavailable", exc_info=True)
        if procs:
            row["background_active"] = len(procs)
            listed = [details.get(p) or {"id": p, "command": "background process"} for p in procs]
            listed.sort(key=lambda d: (d.get("started_at") or 0, d["id"]))
            row["background_processes"] = listed[:_MAX_ROW_PROCESSES]
        else:
            row.pop("background_active", None)
            row.pop("background_processes", None)
    return rows


def process_observations(proc_ids: Iterable[Any], *, owner: str | None, profile: str | None = None,
                         exited: Iterable[Any] = (), owner_resolved: bool = False,
                         scope: tuple[str | None, str | None] | None = None) -> dict[str, dict[str, Any]]:
    """``{registry process id: v1 observation}`` for exact registry processes an owning stored session names.

    Reuses the sidebar's bounded, three-second-cached collection, so the probe time is the original
    kernel-birth probe (a request never renews it). A process absent from that evidence (exited, failed
    probe, not a live host pid) or not owned by ``owner`` gets an explicit unknown observation, never a
    running one (``owner_resolved``: the caller already derived ``owner`` from this process's own
    ids, e.g. a route key resolved to its stored conversation); a non-host pid carries no kernel-birth proof. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    ids = [str(p) for p in proc_ids if p]
    if not ids:
        return out
    try:
        _owned_processes()
        evidence = dict(_evidence)
    except ADAPTER_ERRORS:
        _log.debug("process evidence unavailable", exc_info=True)
        evidence = {}
    try:
        if scope is not None:  # the caller's per-request local_scope(), read once for all its owners
            gateway, local_profile = scope
        else:
            from hermes_cli.kanban_execution_observations import local_scope
            gateway, local_profile = local_scope()
    except ADAPTER_ERRORS:
        _log.debug("process gateway identity unavailable", exc_info=True)
        gateway = local_profile = None
    now = time.time()
    done = {str(p) for p in exited}  # a cached probe from before the exit must not date an exited process
    for proc_id in ids:
        entry = None if proc_id in done else evidence.get(proc_id)
        if entry is None or entry.get("source") != "process.registry" or not owner or not (owner_resolved or owner in entry["owners"]):
            # No probed, owned, live host process: identity only, no source clock.
            entry = {"id": proc_id, "pid": None, "host_start_time": None, "source": "process.registry",
                     "observed_at": None, "pid_scope": "unprobed", "profile": None}
        try:
            out[proc_id] = process(entry, gateway=gateway, profile=profile or entry.get("profile") or local_profile,
                                   session=owner or None, now=now)
        except ADAPTER_ERRORS:
            _log.debug("process execution observation unavailable", exc_info=True)
    return out


def annotate_registry_entries(entries: list[dict[str, Any]], *, owner: str | None,
                              profile: str | None = None) -> list[dict[str, Any]]:
    """Add optional ``execution_observations`` (0/1 items) to each ``process.list`` entry; legacy fields untouched."""
    found = process_observations((e.get("session_id") for e in entries), owner=owner, profile=profile,
                                 exited=(e.get("session_id") for e in entries if e.get("status") == "exited"))
    for entry in entries:
        obs = found.get(str(entry.get("session_id")))
        if obs is not None:
            entry["execution_observations"] = [obs]
    return entries


def _checkpoint_entry(process_id: str) -> dict[str, Any] | None:
    for path in _checkpoint_paths():
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for item in entries if isinstance(entries, list) else ():
            if isinstance(item, dict) and str(item.get("session_id") or "") == process_id:
                return item
    return None


def stop_session_process(process_id: str, session_ids: Iterable[str]) -> dict[str, Any]:
    """Kill one background process a sidebar row owns (``session_ids`` = the
    row's id plus its lineage). A process in this backend's registry goes
    through ``process_registry.kill_process`` — the ``process_manage kill``
    path, so its completion receipt and checkpoint stay right. One supervised
    by another Hermes process (messaging gateway, kanban worker, CLI) is only
    reachable by pid: terminate its tree after re-checking the start time,
    and its owner's reader sees the exit. Returns a ``kill_process``-shaped
    dict (``status``: killed / already_exited / not_found / error)."""
    process_id = str(process_id or "").strip()
    wanted = {str(s).strip() for s in session_ids if str(s or "").strip()}
    if not process_id or not wanted:
        return {"status": "error", "error": "process_id and session_ids are required"}
    try:
        from tools.process_registry import ProcessRegistry, process_registry

        session = process_registry.get(process_id)
        if session is not None:
            item = {"session_key": session.session_key, "parent_session_id": session.parent_session_id,
                    "owner_task_id": session.owner_task_id, "task_id": session.task_id}
            if not _owner_ids(item) & wanted:
                return {"status": "not_found", "error": f"no such process: {process_id}"}
            result = process_registry.kill_process(process_id, source="sidebar.stop")
        else:
            entry = _checkpoint_entry(process_id)
            if entry is None or not _owner_ids(entry) & wanted:
                return {"status": "not_found", "error": f"no such process: {process_id}"}
            pid, start = entry.get("pid"), entry.get("host_start_time")
            if entry.get("pid_scope", "host") != "host" or not _live(pid, start):
                result = {"status": "already_exited", "session_id": process_id}
            else:
                ProcessRegistry._terminate_host_pid(pid, start)
                deadline = time.monotonic() + 3.0
                while _live(pid, start) and time.monotonic() < deadline:
                    time.sleep(0.1)
                result = ({"status": "error", "error": f"pid {pid} is still alive", "session_id": process_id}
                          if _live(pid, start) else {"status": "killed", "session_id": process_id})
    except Exception as exc:  # never 500 the sidebar
        _log.warning("sidebar stop of %s failed", process_id, exc_info=True)
        return {"status": "error", "error": str(exc)}
    _invalidate_cache()  # the next list must not repaint the stopped process
    return result


def _invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


_reset_cache_for_tests = _invalidate_cache
