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
import math
import os
import threading
import time
from pathlib import Path
from typing import Any
from utils import atomic_json_write, is_truthy_value

logger = logging.getLogger(__name__)

_MAX_AGE_SECS = 24 * 3600
_MAX_ENTRIES = 32
# Enough to re-submit any realistic prompt; guards against a multi-megabyte paste being journaled.
_MAX_PROMPT_CHARS = 64_000

# A prompt accepted behind the core-update admission hold (session_auto_continue._queue_submit_behind_update_hold)
# is journaled under this reason so the post-restart ``session.resume`` sends it. It is the user's own unsent
# message, not a turn to redo, so it is not bound by the 15 min turn freshness window: hermes-sync's drain
# window holds admission for up to 15 min and the apply then takes 1-4 min, plus the backend restart and the
# Desktop reconnect. One hour covers that with margin; an older one is dropped (the row stays "not sent").
UPDATE_HOLD_QUEUE_REASON = "update_hold_queue"
UPDATE_HOLD_QUEUE_MAX_AGE_S = 3600.0

# Finite absolute epoch before which this logical turn must not call its provider again (an API-error retry
# wait: a Retry-After the credential pool never benched). Written once per wait, never a duration.
PROVIDER_RETRY_FIELD = "provider_retry_not_before"

_lock = threading.Lock()
_file_lock_holder = threading.local()


def _marker_path(home: Path | str) -> Path:
    return Path(home) / "desktop" / "interrupted_turns.json"


def _cross_process_lock(path: Path):
    """Sibling backends on one HERMES_HOME (#94778) don't share ``_lock``; without this flock one's stale
    load → store resurrects a marker the other just cleared (a finished turn auto-continues again)."""
    from hermes_cli.auth import _file_lock
    return _file_lock(path.with_name(f"{path.name}.lock"), _file_lock_holder, 5.0,
                      f"Timed out waiting for the turn-marker lock ({path})")


AUTO_CONTINUE_FRESHNESS_MINUTES_DEFAULT = 15
AUTO_CONTINUE_MAX_ATTEMPTS_DEFAULT = 2


def auto_continue_bounds(config: Any) -> tuple[bool, float, int]:
    """[daveey] (enabled, freshness window in seconds, max attempts) from a config mapping's
    ``desktop.auto_continue``. The one parse of those bounds: the producer (``_auto_continue_config``)
    and the sidebar's paused-turn reader (``hermes_cli.turn_session_activity``) must agree, or a pause
    that will never continue reads as a live turn."""
    desktop = config.get("desktop") if isinstance(config, dict) else None
    cfg = desktop.get("auto_continue") if isinstance(desktop, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        minutes = float(cfg.get("freshness_minutes", AUTO_CONTINUE_FRESHNESS_MINUTES_DEFAULT))
    except (TypeError, ValueError):
        minutes = float(AUTO_CONTINUE_FRESHNESS_MINUTES_DEFAULT)
    try:
        attempts = int(cfg.get("max_attempts", AUTO_CONTINUE_MAX_ATTEMPTS_DEFAULT))
    except (TypeError, ValueError):
        attempts = AUTO_CONTINUE_MAX_ATTEMPTS_DEFAULT
    return (is_truthy_value(cfg.get("enabled"), default=True), max(0.0, minutes) * 60.0,
            attempts if attempts >= 0 else AUTO_CONTINUE_MAX_ATTEMPTS_DEFAULT)


def finite_deadline(value: Any) -> float | None:
    """``value`` as a finite positive epoch, else None (junk, bool, NaN/inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _started_at(entry: dict) -> float:
    return float(entry.get("started_at") or 0)


def freshness_since(marker: dict) -> float:
    """[daveey] When a marker's auto-continue freshness clock starts: ``started_at`` for a crash-interrupted
    turn ("don't resurrect stale turns"). A core-update pause counts from ``max(started_at, parked_at,
    paused_at)``: the cooperative pause of a live turn restarts the clock at ``paused_at`` (a turn that ran
    80 minutes is not 80 minutes stale when it pauses), and a pause its live writer kept parked between drain
    windows (``mark_turn_parked``) counts from the last time it was confirmed parked. Every reader of
    freshness (in-place continue, startup sweep, notice hold) must agree or a paused turn is dropped."""
    started = _started_at(marker)
    if marker.get("reason") != "core_update_pause":
        return started
    stamps = [started]
    for key in ("parked_at", "paused_at"):
        try:
            stamps.append(float(marker.get(key) or 0))
        except (TypeError, ValueError):
            pass
    return max(stamps)


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
    if entry.get("reason") == UPDATE_HOLD_QUEUE_REASON and entry.get("queue_released") is True:
        return "dead"  # explicit queue-custody handoff, not process death
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
        path = _marker_path(home)
        with _lock, _cross_process_lock(path):
            entries = mutate(_load(path))
            if entries is not None:
                _store(path, entries)
                return True
    except Exception:
        logger.warning("failed to %s turn marker for %s", what, session_key, exc_info=True)
    return False


def release_update_queue(home: Path | str, session_key: str, marker: dict) -> bool:
    """Release only this process's unchanged idle queue marker for another backend."""
    def mutate(entries):
        entry = entries.get(session_key)
        if (not isinstance(entry, dict) or entry.get("reason") != UPDATE_HOLD_QUEUE_REASON
                or entry.get("writer_pid") != os.getpid()
                or entry.get("started_at") != marker.get("started_at")
                or entry.get("queue_released")):
            return None
        entry["queue_released"] = True
        return entries

    return _update(home, session_key, mutate, "release queued prompt")


def record_turn_start(home: Path | str, session_key: str, prompt: str, *, attempts: int = 0,
                      auto_continue: bool = True, notification_category: str | None = None,
                      pauses: int = 0, killed_jobs: list | None = None, until_tool: bool = False,
                      reason: str | None = None, carried_job_ids=(),
                      provider_retry_not_before: float | None = None) -> bool:
    """Persist the marker for a turn that is about to run. ``attempts`` = how many auto-continues led to
    this run (0 for a user-initiated turn); the crash-loop breaker reads it back on the next resume.
    ``pauses`` = core-update pauses this logical turn already took (``mark_turn_paused``), counted apart
    from crash attempts so a pause never uses one up. ``until_tool`` [daveey]: the turn is continuable only
    until its first tool starts (``mark_turn_tool_started``); used by at-most-once admitted decisions.
    [daveey] An exit-journal receipt marker already on disk for this key (a backend exit journaled while this
    turn was starting) keeps its receipts in the same locked write, except ``carried_job_ids`` (notices this
    turn's prompt already carries); the receipts ``killed_jobs`` passes win on a shared id.
    True only when the marker write landed."""
    if not session_key or not prompt:
        return False
    now = time.time()
    entry = {"attempts": max(0, int(attempts)), "prompt": prompt[:_MAX_PROMPT_CHARS], "started_at": now,
             "auto_continue": bool(auto_continue), **_writer_identity(),
             # [daveey] This backend's pause restarts the freshness clock (freshness_since), so hermes-sync may
             # ask this turn to pause whatever its age. An older backend's marker lacks the field.
             "pause_restarts_freshness": True}
    if killed_jobs:
        entry["killed_jobs"] = list(killed_jobs)
    if pauses:
        entry["pauses"] = max(0, int(pauses))
    if until_tool:
        entry["until_tool"] = True
    if notification_category == "diagnostic":
        entry["notification_category"] = notification_category
    if reason:
        entry["reason"] = str(reason)
    # Only a logical continuation passes this; a fresh user turn's entry never carries a wait.
    if (retry_deadline := finite_deadline(provider_retry_not_before)) is not None:
        entry[PROVIDER_RETRY_FIELD] = retry_deadline
    # Identity only — never the prompt: this log is read on crash triage and must not carry turn content.
    logger.debug("turn marker recorded for session %s by writer pid %s", session_key, entry["writer_pid"])
    carried = {str(i) for i in carried_job_ids or ()}

    def mutate(entries):
        prior = entries.get(session_key)
        if (reason == UPDATE_HOLD_QUEUE_REASON and isinstance(prior, dict) and not prior.get("reason")
                and prior.get("writer_pid") != entry["writer_pid"] and marker_writer_state(prior) != "dead"):
            # [daveey] A sibling backend on this HERMES_HOME (#94778) is mid-turn on this session: its live-turn
            # marker is what keeps hermes-sync from updating over that turn, and hermes-sync skips hold-queue
            # markers as "not running". Never replace it; that backend's own end-of-turn clears it.
            return None
        if isinstance(prior, dict) and prior.get("reason") == _JOBS_REASON:
            own = {str(j.get("session_id")) for j in entry.get("killed_jobs", ()) if isinstance(j, dict)}
            kept = [j for j in prior.get("killed_jobs") or () if isinstance(j, dict)
                    and str(j.get("session_id") or "") not in own | carried]
            if kept:
                entry["killed_jobs"] = kept + list(entry.get("killed_jobs") or ())
                entry["exit_receipt_ids"] = [str(j.get("session_id")) for j in kept]
        return {**_prune(entries, now), session_key: entry}

    return _update(home, session_key, mutate, "record")


# Mirrors exit_job_receipts.JOBS_REASON (imported there; kept literal here to avoid an import cycle).
_JOBS_REASON = "background_jobs_stopped"


def retire_cancelled_turn_marker(home: Path | str, session_key: str) -> bool:
    """[daveey] A turn Stopped before its marker landed: drop the turn, but exit-journal receipts its marker
    absorbed (``exit_receipt_ids``) go back to a receipt marker instead of being deleted with it."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict):
            return None
        ids = set(entry.get("exit_receipt_ids") or ())
        kept = [j for j in entry.get("killed_jobs") or () if isinstance(j, dict) and j.get("session_id") in ids]
        rest = {k: v for k, v in entries.items() if k != session_key}
        if not kept:
            return rest
        from tui_gateway.exit_job_receipts import JOBS_PROMPT
        rest[session_key] = {"attempts": 0, "prompt": JOBS_PROMPT, "started_at": time.time(), "auto_continue": True,
                             **_writer_identity(), "pause_restarts_freshness": True, "reason": _JOBS_REASON,
                             "paused_at": time.time(), "killed_jobs": kept}
        return rest

    return bool(session_key) and _update(home, session_key, mutate, "retire")


def merge_receipts(home: Path | str, session_key: str, jobs: list, merge, *, create: bool,
                   prompt: str) -> bool:
    """[daveey] One locked load → merge → store of exit receipts into the session's marker (no read/write gap a
    concurrent ``record_turn_start`` could fall into). No marker: a receipt marker is created only when
    ``create``. True when the write landed."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict):
            if not create:
                return None
            now = time.time()
            entry = entries[session_key] = {"attempts": 0, "prompt": prompt, "started_at": now,
                                             "auto_continue": True, **_writer_identity(),
                                             "pause_restarts_freshness": True, "reason": _JOBS_REASON,
                                             "paused_at": now}
        prior = _paused_killed_jobs(entry)
        merged = merge(prior, jobs)
        entry["killed_jobs"] = merged
        if entry.get("reason") != _JOBS_REASON:
            prior_ids = {j["session_id"] for j in prior}
            extra = [j["session_id"] for j in jobs if j["session_id"] not in prior_ids]
            if entry.get("exit_receipt_ids") is not None or extra:
                entry["exit_receipt_ids"] = sorted(set(entry.get("exit_receipt_ids") or ()) | set(extra))
        return entries

    return bool(session_key) and _update(home, session_key, mutate, "journal jobs")


def clear_turn_marker(home: Path | str, session_key: str, *, concluding_turn: frozenset | None = None,
                      owner_only: bool = False) -> None:
    """Remove a concluded marker. A late backstop must name its owner; compare and delete under the same lock.
    [daveey] ``owner_only``: delete only a marker this process wrote or whose writer is dead; a live sibling's
    marker is its running turn's (a refused submit in pid 399307 deleted pid 491642's, 2026-11-20)."""
    def mutate(entries):
        if session_key not in entries:
            return None
        entry = entries[session_key]
        if owner_only and entry.get("writer_pid") != os.getpid() and marker_writer_state(entry) != "dead":
            logger.info("turn marker for session %s kept: writer pid %s is not this process (%s) nor known dead",
                        session_key, entry.get("writer_pid"), os.getpid())
            return None
        if concluding_turn is not None:
            from hermes_state_outcome import turn_identity
            identity = turn_identity(marker=entries[session_key])
            if not identity or not identity <= concluding_turn:
                return None
        return {k: v for k, v in entries.items() if k != session_key}

    if session_key:
        _update(home, session_key, mutate, "clear")


def mark_turn_tool_started(home: Path | str, session_key: str) -> bool:
    """[daveey] An ``until_tool`` turn just started its first tool: from here a restart may have left a side
    effect behind, so auto-continue is switched off (the turn's at-most-once contract). Written BEFORE the tool
    runs. True when an ``until_tool`` marker was switched off."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict) or not entry.get("until_tool") or not entry.get("auto_continue", True):
            return None
        entry.update(auto_continue=False, tool_started_at=time.time())
        return entries

    return bool(session_key) and _update(home, session_key, mutate, "mark tool start")


def mark_turn_paused(home: Path | str, session_key: str, reason: str,
                     *, killed_jobs: list | None = None, increment: bool = True, carried: bool = False) -> bool:
    """[daveey] Stamp a live turn's marker as paused at a step boundary for a core update: ``reason``,
    ``paused_at``, ``pauses`` + 1, and this process as its writer. ``auto_continue`` stays as recorded.
    ``killed_jobs`` are the jobs this pause actually stopped; they ride the same write so a resume
    after restart can list them. ``carried``: a synthesized turn running over this paused marker paid the
    pause (``carried_turn_paused``, read by the continuation note). True when a marker existed to stamp."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict):
            return None
        entry.update(reason=str(reason), paused_at=time.time(), pauses=int(entry.get("pauses") or 0) + int(increment),
                     **_writer_identity(), **({"carried_turn_paused": True} if carried else {}))
        if killed_jobs:
            entry["killed_jobs"] = list(killed_jobs)
        return entries

    if session_key:
        return _update(home, session_key, mutate, "pause")
    return False


def _is_own_generation(entry: Any, generation: Any) -> bool:
    """``entry`` is the marker THIS process's turn wrote: same ``started_at`` and writer identity as the one
    captured when the turn recorded it. A carried, successor or foreign-writer marker never matches."""
    if not isinstance(entry, dict) or not isinstance(generation, dict):
        return False
    started = generation.get("started_at")
    if not started or entry.get("started_at") != started:
        return False
    me = _writer_identity()
    if entry.get("writer_pid") != me["writer_pid"] or generation.get("writer_pid") != me["writer_pid"]:
        return False
    return entry.get("writer_start_time") == me.get("writer_start_time") == generation.get("writer_start_time")


def stamp_provider_retry_deadline(home: Path | str, session_key: str, deadline: float, generation: dict | None) -> bool:
    """[daveey] Durably record this turn's absolute retry deadline on its OWN marker. Fails closed (False) for an
    empty key, a missing/carried/successor/foreign marker, a different still-active deadline (first write wins)
    and any write or readback mismatch; the caller then keeps the ordinary in-process wait and never yields.
    True only after the projected marker reads back the exact deadline under the same generation."""
    want = finite_deadline(deadline)
    if not session_key or want is None or not generation:
        return False

    def mutate(entries):
        entry = entries.get(session_key)
        if not _is_own_generation(entry, generation):
            return None
        current = finite_deadline(entry.get(PROVIDER_RETRY_FIELD))
        if current is not None and current > time.time() and current != want:
            return None
        entry[PROVIDER_RETRY_FIELD] = want
        return entries

    if not _update(home, session_key, mutate, "stamp retry deadline"):
        return False
    back = read_turn_marker(home, session_key)
    return bool(back) and back.get(PROVIDER_RETRY_FIELD) == want and back.get("started_at") == generation["started_at"] \
        and back.get("writer_pid") == generation.get("writer_pid")


def clear_provider_retry_deadline(home: Path | str, session_key: str, deadline: float, generation: dict | None) -> bool:
    """Best-effort: drop ``deadline`` from this turn's own marker once served (only that exact value)."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not _is_own_generation(entry, generation) or entry.get(PROVIDER_RETRY_FIELD) != deadline:
            return None
        entry.pop(PROVIDER_RETRY_FIELD, None)
        return entries

    return bool(session_key) and _update(home, session_key, mutate, "clear retry deadline")


def clear_carried_turn_paused(home: Path | str, session_key: str) -> bool:
    """[daveey] Take back a write-ahead ``carried_turn_paused`` stamp whose carried pause did not stand (the job
    stop failed, or a user Stop won), so the continuation does not name a notification turn that ran to completion
    or was stopped. Everything else untouched. True when a stamp was removed."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict) or not entry.pop("carried_turn_paused", None):
            return None
        return entries

    return bool(session_key) and _update(home, session_key, mutate, "unstamp carried pause")


def retire_paused_marker(home: Path | str, session_key: str, paused_at: float | None) -> bool:
    """[daveey] Drop a core-update pause whose continuation was handed to the chat's owner or retired
    (t_01a1d4920ce3cbf4). Compare and delete under one lock: only the pause stamped ``paused_at`` goes, so a
    later turn's own marker or a newer pause of the same turn survives. True when it was removed."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict) or entry.get("reason") != "core_update_pause":
            return None
        try:
            if paused_at is None or float(entry.get("paused_at") or 0.0) != float(paused_at):
                return None
        except (TypeError, ValueError):
            return None
        return {k: v for k, v in entries.items() if k != session_key}

    return bool(session_key) and _update(home, session_key, mutate, "retire pause")


def mark_turn_parked(home: Path | str, session_key: str) -> bool:
    """[daveey] Restamp ``parked_at`` on a core-update pause its live writer keeps idle between drain
    windows (``update_pause.drain_retry_pending``). Reason, prompt and counters untouched."""
    def mutate(entries):
        entry = entries.get(session_key)
        if not isinstance(entry, dict) or entry.get("reason") != "core_update_pause":
            return None
        entry["parked_at"] = time.time()
        return entries

    return bool(session_key) and _update(home, session_key, mutate, "park")


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
    """Exit/pause receipts, including ``kill_state=completed`` pending notices; [] for junk."""
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
                **({"queue_released": True} if entry.get("queue_released") is True else {}),
                **({"reason": str(entry["reason"])} if entry.get("reason") else {}),
                **({"parked_at": float(entry["parked_at"])} if entry.get("parked_at") else {}),
                **({"paused_at": float(entry["paused_at"])} if entry.get("paused_at") else {}),
                **({"until_tool": True} if entry.get("until_tool") else {}),
                **({"carried_turn_paused": True} if entry.get("carried_turn_paused") else {}),
                **({PROVIDER_RETRY_FIELD: d} if (d := finite_deadline(entry.get(PROVIDER_RETRY_FIELD))) else {}),
                **({"notification_category": "diagnostic"}
                   if entry.get("notification_category") == "diagnostic" else {}),
                **({"killed_jobs": killed} if killed else {})}
    except Exception:
        return None
