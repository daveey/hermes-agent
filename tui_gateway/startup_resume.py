"""Resume every crash/restart-interrupted Desktop turn when the backend starts.

Auto-continue (``session_auto_continue._maybe_schedule_auto_continue``) only fires from ``session.resume``, i.e. when
the user re-opens each interrupted session. After an app restart or ``hermes update`` that means visiting every
session that was mid-turn and telling it to continue. With ``desktop.auto_continue.resume_on_startup: true`` the
backend does that itself at boot: for each fresh turn marker (``turn_marker``) whose writer process is gone, it runs
an in-process ``session.resume`` — the same handler the Desktop calls — which schedules the continuation turn. The
sessions run detached until a client attaches; opening one later reuses the live session and streams the turn.

All the existing auto-continue guards still apply inside ``session.resume`` (freshness window, ``max_attempts``
crash-loop breaker, live-writer ownership, hosted-room exclusion); this module only pre-filters to avoid resuming
sessions that would not continue anyway.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from tui_gateway.exit_job_receipts import JOBS_REASON, JOBS_RECEIPT_MAX_AGE_S
from tui_gateway.turn_marker import (
    UPDATE_HOLD_QUEUE_MAX_AGE_S,
    UPDATE_HOLD_QUEUE_REASON,
    _load,
    _marker_path,
    marker_writer_state,
    read_turn_marker,
)

logger = logging.getLogger(__name__)

_ids = itertools.count(1)
# [daveey] Markers the boot sweep retries while an old writer may still be exiting: core-update pauses and
# background-job receipts (``exit_job_receipts``), and prompts queued behind the update hold. A receipt and a
# hold-queued prompt also outlive the turn freshness window.
_RETRY_REASONS = frozenset({"core_update_pause", JOBS_REASON, UPDATE_HOLD_QUEUE_REASON})


def _max_age(marker: dict, fresh: float) -> float:
    if marker.get("reason") == JOBS_REASON:
        return JOBS_RECEIPT_MAX_AGE_S
    if marker.get("reason") == UPDATE_HOLD_QUEUE_REASON:
        return max(fresh, UPDATE_HOLD_QUEUE_MAX_AGE_S)
    return fresh


def _settings(cfg: dict | None) -> tuple[bool, float, int, float]:
    """(resume_on_startup, freshness seconds, max_attempts, startup delay seconds) from ``desktop.auto_continue``."""
    desktop = (cfg or {}).get("desktop")
    ac = desktop.get("auto_continue") if isinstance(desktop, dict) else None
    ac = ac if isinstance(ac, dict) else {}
    enabled = ac.get("enabled", True) not in (False, "false", "False", 0, "0", "off", "no")
    on_start = ac.get("resume_on_startup", False) in (True, "true", "True", 1, "1", "on", "yes")
    try:
        fresh = max(0.0, float(ac.get("freshness_minutes", 15))) * 60.0
    except (TypeError, ValueError):
        fresh = 15 * 60.0
    try:
        attempts = max(0, int(ac.get("max_attempts", 2)))
    except (TypeError, ValueError):
        attempts = 2
    try:
        delay = max(0.0, float(ac.get("resume_on_startup_delay_seconds", 5)))
    except (TypeError, ValueError):
        delay = 5.0
    return enabled and on_start, fresh, attempts, delay


def _homes(home: Path) -> list[tuple[str | None, Path]]:
    """The launch home plus every named profile under it: markers are stored per HERMES_HOME."""
    out: list[tuple[str | None, Path]] = [(None, home)]
    with_profiles = home / "profiles"
    if with_profiles.is_dir():
        for p in sorted(with_profiles.iterdir()):
            if p.is_dir() and _marker_path(p).exists():
                out.append((p.name, p))
    return out


def _skip_reason(m: dict | None, now: float, fresh: float, max_attempts: int) -> str | None:
    """Why the sweep leaves this marker alone, or None when it resumes it."""
    if m is None:
        return "unreadable or empty prompt"
    if not m.get("auto_continue", True):
        return "auto_continue off (a tool already ran in an at-most-once turn, or its owner recovers it)"
    if now - m["started_at"] > _max_age(m, fresh):
        return f"older than the {_max_age(m, fresh) / 60:.0f} min freshness window"
    if m["attempts"] >= max_attempts:
        return f"attempt cap reached ({m['attempts']}/{max_attempts})"
    if marker_writer_state(m) == "alive":
        return f"writer pid {m.get('writer_pid')} is still alive"
    return None


def pending_resumes(home: Path, cfg: dict | None, *, now: float | None = None,
                    log_skips: bool = True) -> list[dict[str, Any]]:
    """Markers the sweep would resume: fresh, auto-continuable, under the attempt cap, writer process gone."""
    _, fresh, max_attempts, _ = _settings(cfg)
    now = time.time() if now is None else now
    out = []
    for profile, h in _homes(home):
        for key in list(_load(_marker_path(h))):
            m = read_turn_marker(h, key)
            # [daveey] Every skip is logged with its reason: a silent skip hid a lost decision (desktop-3a6213ce74).
            if (why := _skip_reason(m, now, fresh, max_attempts)) is not None:
                if log_skips:
                    logger.info("startup resume: marker %s (profile=%s) skipped: %s", key, profile or "default", why)
                continue
            out.append({"profile": profile, "home": str(h), "session_key": key, "started_at": m["started_at"]})
    out.sort(key=lambda r: r["started_at"], reverse=True)
    return out


def resume_interrupted_sessions(resume: Callable[[str, dict], Any], home: Path, cfg: dict | None,
                                *, paused_only: bool = False) -> list[dict]:
    """Call ``resume(rid, params)`` (the ``session.resume`` handler) for each pending marker; returns outcomes."""
    results = []
    for row in pending_resumes(home, cfg, log_skips=not paused_only):
        if paused_only and (read_turn_marker(row["home"], row["session_key"]) or {}).get("reason") not in _RETRY_REASONS:
            continue
        # [daveey] No client is attached: run as the session's stored source, not this process's env default.
        params: dict[str, Any] = {"session_id": row["session_key"], "omit_messages": True, "source_from_store": True}
        if row["profile"]:
            params["profile"] = row["profile"]
        try:
            env = resume(f"startup-resume-{next(_ids)}", params) or {}
        except Exception as exc:  # one bad session must not stop the rest
            logger.warning("startup resume of %s failed", row["session_key"], exc_info=True)
            results.append({**row, "ok": False, "error": str(exc)})
            continue
        err = env.get("error") if isinstance(env, dict) else None
        res = env.get("result") if isinstance(env, dict) else None
        scheduled = bool(isinstance(res, dict) and res.get("auto_continue"))
        logger.info("startup resume %s (profile=%s): %s", row["session_key"], row["profile"] or "default",
                    "auto-continue scheduled" if scheduled else (err or "resumed, no continuation scheduled"))
        results.append({**row, "ok": err is None, "scheduled": scheduled, **({"error": err} if err else {})})
    return results


def _paused_markers_remain(home: Path, cfg: dict | None) -> bool:
    # Include live writers: they can still be retiring during the first boot sweep.
    _, fresh, max_attempts, _ = _settings(cfg)
    now = time.time()
    return any(entry.get("reason") in _RETRY_REASONS and entry.get("auto_continue", True)
               and now - float(entry.get("started_at") or 0) <= _max_age(entry, fresh)
               and int(entry.get("attempts") or 0) < max_attempts
               for _, h in _homes(home) for key in _load(_marker_path(h))
               if (entry := read_turn_marker(h, key)) is not None)


def start_startup_resume_sweep() -> threading.Thread | None:
    """Backend boot hook: run the sweep on a daemon thread after a short settle delay (no-op unless enabled)."""
    try:
        from hermes_cli.config import load_config
        from hermes_constants import get_hermes_home
        cfg = load_config()
    except Exception:
        logger.debug("startup resume: config unavailable", exc_info=True)
        return None
    enabled, _, _, delay = _settings(cfg)
    if not enabled:
        return None

    home = Path(get_hermes_home())

    def _run() -> None:
        time.sleep(delay)
        try:
            from tui_gateway import server
            handler = server._methods.get("session.resume")
            if handler is None:
                logger.warning("startup resume: session.resume handler not installed")
                return
            done = resume_interrupted_sessions(handler, home, cfg)
            if done:
                logger.info("startup resume: %d interrupted session(s) processed", len(done))
            # Backend boot can precede hold release or the last old SSH writer's exit.
            # Retry PAUSES only: ordinary crash recovery keeps its one-shot semantics.
            deadline = time.monotonic() + 600.0
            while time.monotonic() < deadline and _paused_markers_remain(home, cfg):
                time.sleep(5.0)
                resume_interrupted_sessions(handler, home, cfg, paused_only=True)
            if _paused_markers_remain(home, cfg):
                logger.warning("startup resume: paused turns still pending after the update recovery window")
        except Exception:
            logger.exception("startup resume sweep failed")

    from agent.memory_provider import spawn_context_thread
    t = spawn_context_thread(_run, name="startup-resume-sweep")
    t.start()
    return t
