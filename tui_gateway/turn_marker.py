"""Durable interrupted-turn markers for the desktop/TUI auto-continue path. A running turn's progress
lives only in process memory (the agent flushes to SQLite at turn end), so a marker is written at turn
start and cleared on any conclusion — only a process death leaves one behind, and ``session.resume``
reads it (``_maybe_schedule_auto_continue``). Stored per ``HERMES_HOME`` (profile-aware); writes prune
entries older than ``_MAX_AGE_SECS`` and cap the count so a crash streak can't grow the file. Every
function is best-effort — marker bookkeeping must never break a turn — so I/O errors degrade to "no
marker" instead of raising. A marker also carries its writer's pid + start time (``marker_writer_state``): "a
marker exists" only implies the writer died when no live sibling backend wrote it (#94778)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_MAX_AGE_SECS = 24 * 3600
_MAX_ENTRIES = 32
# Enough to re-submit any realistic prompt; guards against a multi-megabyte paste being journaled.
_MAX_PROMPT_CHARS = 64_000

_lock = threading.Lock()


def _marker_path(home: Path | str) -> Path:
    return Path(home) / "desktop" / "interrupted_turns.json"


def _started_at(entry: dict) -> float:
    return float(entry.get("started_at") or 0)


def _writer_identity() -> dict:
    """Best-effort identity of the process writing this marker. ``writer_pid`` alone already answers "who wrote
    this, and are they still alive?" (``marker_writer_state``); ``writer_start_time`` pairs the pid with its create
    time so a recycled pid cannot pass as the original writer. Identity is bookkeeping, never turn-critical, so
    every failure degrades to a bare pid."""
    identity = {"writer_pid": os.getpid()}
    try:
        from hermes_cli.active_sessions import _own_start_time
        start = _own_start_time()
        if start is not None:
            identity["writer_start_time"] = float(start)
    except Exception:
        pass
    return identity


def marker_writer_state(entry: dict) -> str:
    """``"alive"`` / ``"dead"`` / ``"unknown"``: is the process that wrote this marker still running?

    A marker is durable proof a turn started — never proof its writer died. Two backends sharing one HERMES_HOME
    break that assumption: A is mid-turn on session S while B resumes S, and B used to read A's marker as crash
    evidence and start a second turn over it (#94778). Liveness comes from ``active_sessions._pid_liveness``
    (pid + start time, so a reused pid reads dead), and "unknown" is the safe answer: it leaves the marker alone
    without claiming its writer is gone.
    """
    if not isinstance(entry, dict):
        return "unknown"
    pid = entry.get("writer_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    try:
        from hermes_cli.active_sessions import _pid_liveness
        live = _pid_liveness(pid, entry.get("writer_start_time"))
    except Exception:
        return "unknown"
    return "unknown" if live is None else ("alive" if live else "dead")


def _load(path: Path) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("unreadable turn-marker file %s; starting fresh", path, exc_info=True)
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _prune(entries: dict[str, dict], now: float) -> dict[str, dict]:
    fresh = {k: e for k, e in entries.items() if now - _started_at(e) <= _MAX_AGE_SECS}
    if len(fresh) <= _MAX_ENTRIES:
        return fresh
    return dict(sorted(fresh.items(), key=lambda item: _started_at(item[1]), reverse=True)[:_MAX_ENTRIES])


def _store(path: Path, entries: dict[str, dict]) -> None:
    if not entries:
        path.unlink(missing_ok=True)
        return
    atomic_json_write(path, entries, indent=None, mode=0o600)


def _update(home: Path | str, session_key: str, mutate, what: str) -> bool:
    """Load → ``mutate(entries)`` → store under the lock; ``mutate`` returns None to skip the write."""
    try:
        with _lock:
            path = _marker_path(home)
            entries = mutate(_load(path))
            if entries is not None:
                _store(path, entries)
                return True
    except Exception:
        logger.warning("failed to %s turn marker for %s", what, session_key, exc_info=True)
    return False


def record_turn_start(home: Path | str, session_key: str, prompt: str, *, attempts: int = 0,
                      auto_continue: bool = True, notification_category: str | None = None,
                      pauses: int = 0, killed_jobs: list | None = None) -> None:
    """Persist the marker for a turn that is about to run. ``attempts`` = how many auto-continues led to
    this run (0 for a user-initiated turn); the crash-loop breaker reads it back on the next resume.
    ``pauses`` = core-update pauses this logical turn already took (``mark_turn_paused``), counted apart
    from crash attempts so a pause never uses one up."""
    if not session_key or not prompt:
        return
    now = time.time()
    entry = {"attempts": max(0, int(attempts)), "prompt": prompt[:_MAX_PROMPT_CHARS], "started_at": now,
             "auto_continue": bool(auto_continue), **_writer_identity()}
    if killed_jobs:
        entry["killed_jobs"] = list(killed_jobs)
    if pauses:
        entry["pauses"] = max(0, int(pauses))
    if notification_category == "diagnostic":
        entry["notification_category"] = notification_category
    # Identity only — never the prompt: this log is read on crash triage and must not carry turn content.
    logger.debug("turn marker recorded for session %s by writer pid %s", session_key, entry["writer_pid"])
    _update(home, session_key, lambda entries: {**_prune(entries, now), session_key: entry}, "record")


def clear_turn_marker(home: Path | str, session_key: str) -> None:
    """Remove the marker once its turn concluded (any outcome the client saw)."""
    if session_key:
        _update(home, session_key, lambda e: {k: v for k, v in e.items() if k != session_key} if session_key in e else None, "clear")


def mark_turn_paused(home: Path | str, session_key: str, reason: str,
                     *, killed_jobs: list | None = None, increment: bool = True) -> bool:
    """[daveey] Stamp a live turn's marker as paused at a step boundary for a core update: ``reason``,
    ``paused_at``, ``pauses`` + 1, and this process as its writer. ``auto_continue`` stays as recorded.
    ``killed_jobs`` are the jobs this pause actually stopped; they ride the same write so a resume
    after restart can list them. True when a marker existed to stamp."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict):
            return None
        entry.update(reason=str(reason), paused_at=time.time(), pauses=int(entry.get("pauses") or 0) + int(increment),
                     **_writer_identity())
        if killed_jobs:
            entry["killed_jobs"] = list(killed_jobs)
        return entries

    if session_key:
        return _update(home, session_key, mutate, "pause")
    return False


def record_killed_jobs(home: Path | str, session_key: str, killed_jobs: list) -> bool:
    """[daveey] Replace an existing marker's ``killed_jobs`` (reason, prompt and counters untouched).
    True when a marker existed and the write landed."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict):
            return None
        entry["killed_jobs"] = list(killed_jobs)
        return entries

    return bool(session_key) and _update(home, session_key, mutate, "journal jobs")


def _paused_killed_jobs(entry: dict) -> list[dict]:
    """Jobs a core-update pause actually stopped, or [] when the marker has none / junk."""
    raw = entry.get("killed_jobs")
    if not isinstance(raw, list):
        return []
    jobs = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("session_id") or "")
        if not sid:
            continue
        try:
            started = float(item.get("started_at") or 0)
        except (TypeError, ValueError):
            started = 0.0
        job = {"session_id": sid, "command": str(item.get("command") or ""), "started_at": started,
               "notify_on_complete": bool(item.get("notify_on_complete")),
               "kill_state": str(item.get("kill_state") or "killed")}
        for key in ("notice", "kill_reason", "termination_source"):
            if item.get(key):
                job[key] = str(item[key])
        if item.get("exit_code") is not None:
            job["exit_code"] = item["exit_code"]
        if item.get("output"):
            job["output"] = str(item["output"])
        jobs.append(job)
    return jobs


def read_turn_marker(home: Path | str, session_key: str) -> dict[str, Any] | None:
    """The marker left by a turn that never concluded, or None."""
    if not session_key:
        return None
    try:
        with _lock:
            entry = _load(_marker_path(home)).get(session_key)
        if not isinstance(entry, dict):
            return None
        prompt = str(entry.get("prompt") or "")
        if not prompt.strip():
            return None
        killed = _paused_killed_jobs(entry)
        return {"attempts": max(0, int(entry.get("attempts") or 0)), "prompt": prompt, "started_at": _started_at(entry),
                "auto_continue": bool(entry.get("auto_continue", True)),
                "pauses": max(0, int(entry.get("pauses") or 0)),
                # Writer identity when present: extra keys only, so a marker written by an older build still reads.
                **{k: entry[k] for k in ("writer_pid", "writer_start_time") if entry.get(k) is not None},
                **({"reason": str(entry["reason"])} if entry.get("reason") else {}),
                **({"notification_category": "diagnostic"}
                   if entry.get("notification_category") == "diagnostic" else {}),
                **({"killed_jobs": killed} if killed else {})}
    except Exception:
        return None
