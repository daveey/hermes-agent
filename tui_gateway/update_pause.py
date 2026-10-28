"""[daveey] Core-update pause for Desktop/TUI turns (t_01a1827558218fed).

hermes-sync's drain window holds update admission while the turns in flight finish, so one long turn used
to hold an update for the whole window, again and again. Once the window has waited a while and every
remaining turn is one the updated backend auto-continues, the drainer writes a pause request beside the
hold (``hermes_cli.admission_fence.pause_requested``). Each such turn then ends at its next step boundary
(after its tool results are persisted, before the next model call; ``agent.turn_iteration_prep``), keeps
its interrupted-turn marker stamped ``reason: core_update_pause``, and releases its admission reservation,
so the update applies. The next backend's startup sweep continues the turn with ``pause_note`` and no
crash-resume attempt is used. Pauses are counted apart (``MAX_PAUSES`` per turn) so they cannot loop.

A turn the next backend would not auto-continue is never paused. A paused turn whose window ends without
an update (expired, apply refused) continues in place (``start_in_place_watcher``), so it never hangs.
When the update did move the checkout, the turn is left for a successor only while one is coming: this
process's skew watchdog runs and its idle proof holds (or a retirement is already under way). A backend
kept busy by other work for ``NO_REPLACEMENT_S``, or one no watchdog retires, continues the turn here.
The retirement fence makes the two exclusive: in-place work is admitted through it, so once retirement
prepares or commits, the turn is left to the successor's startup sweep.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PAUSE_REASON = "core_update_pause"
MAX_PAUSES = 3
PAUSE_STATUS_TEXT = "Paused for a Hermes update; continues automatically"
# The update itself takes a few minutes: a turn that would age out of the freshness window meanwhile is
# not paused (mirrors hermes-sync's AUTO_CONTINUE_UPDATE_MARGIN_S).
UPDATE_MARGIN_S = 600.0
WATCH_POLL_S = 5.0
WATCH_MAX_S = 6 * 3600.0
# A skewed backend normally retires once idle and its successor continues the turn. One that is still
# here this long after the checkout moved has no working retirement path: continue in place (backstop).
LEAVE_GRACE_S = 300.0
# [daveey] t_01a18300e42c3364: the skew watchdog retires a backend only through the idle proof, so a
# backend with other work in flight (another chat's turn, a pending prompt, an unreadable ledger) is
# not replaced while that lasts, and one with no watchdog (a stdio TUI) is never replaced. A paused
# turn used to wait the whole grace (900 s) for that successor. Now it waits only while a replacement
# is actually on its way; once none has been for this long, it continues here.
NO_REPLACEMENT_S = 60.0


def _now() -> float:
    return time.time()


def _home(session: dict) -> Path:
    from tui_gateway import server
    return Path(session.get("profile_home") or server._hermes_home)


def _root(session: dict) -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root(home=_home(session))


def pause_requested(session: dict) -> bool:
    from hermes_cli.admission_fence import pause_requested as requested
    try:
        return requested(_root(session))
    except Exception:
        return False


def not_resumable_reason(session: dict, marker_key: str) -> str | None:
    """None when the next backend will auto-continue this turn (the startup sweep's rules); else why not."""
    from tui_gateway import server
    from tui_gateway.startup_resume import _settings
    from tui_gateway.turn_marker import read_turn_marker
    if session.get("source") == "bot_room":
        return "hosted room turn"
    try:
        on_startup, fresh_s, max_attempts, _ = _settings(server._load_cfg())
    except Exception:
        return "config unreadable"
    if not on_startup:
        return "desktop.auto_continue.resume_on_startup is off"
    marker = read_turn_marker(_home(session), marker_key)
    if marker is None:
        return "no turn marker"
    if not marker["auto_continue"]:
        return "turn opted out of auto-continue"
    if marker["attempts"] >= max_attempts:
        return "crash-resume attempts used up"
    if marker["pauses"] >= MAX_PAUSES:
        return f"already paused {marker['pauses']} times"
    if _now() - marker["started_at"] + UPDATE_MARGIN_S > fresh_s:
        return "turn older than the auto-continue freshness window"
    return None


def boundary_pause_hook(session: dict, marker_key: str) -> Callable[[], str | None]:
    """The per-turn ``agent.step_boundary_pause``: one stat per step while no pause is requested."""
    declined: list[str] = []

    def hook() -> str | None:
        if not marker_key or not pause_requested(session):
            return None
        why = not_resumable_reason(session, marker_key)
        if why:
            if not declined:
                declined.append(why)
                logger.info("core-update pause: session %s keeps running (%s)", marker_key, why)
            return None
        from tui_gateway.turn_marker import mark_turn_paused
        if not mark_turn_paused(_home(session), marker_key, PAUSE_REASON):
            return None
        with session["history_lock"]:
            session["_update_pause_marker_key"] = marker_key
        logger.info("core-update pause: session %s paused at a step boundary for a Hermes update", marker_key)
        return PAUSE_REASON

    return hook


def pause_note(prompt: str) -> str:
    from tui_gateway.session_history import _AUTO_CONTINUE_NOTE_PREFIX
    return (f"{_AUTO_CONTINUE_NOTE_PREFIX} — it was paused at a step boundary for a Hermes update. Every "
            "tool result above is complete; continue with the next step and don't redo finished steps. "
            f"The paused request was:]\n\n{prompt}")


def announce_pause(sid: str, payload: dict) -> None:
    """Make the pause visible: a status line plus the terminal frame's text."""
    from tui_gateway import server
    server._emit("status.update", sid, {"kind": "process", "text": PAUSE_STATUS_TEXT})
    text = payload.get("text")
    payload["text"] = f"{text}\n\n{PAUSE_STATUS_TEXT}" if isinstance(text, str) and text.strip() else PAUSE_STATUS_TEXT
    payload.pop("rendered", None)
    payload["paused_for_update"] = True


def _detect_skew():
    from gateway.code_skew import detect_code_skew
    return detect_code_skew()


def in_place_resume_action(session: dict, marker_key: str, *, skew: Callable[[], Any] | None = None) -> str:
    """What a paused turn's own backend does now: ``wait`` (the window is still up), ``leave`` (the
    checkout moved: the next backend continues it), ``resume`` (no update replaced us: continue here) or
    ``done`` (the user or another path already moved on)."""
    from hermes_cli.admission_fence import is_held
    from tui_gateway.turn_marker import read_turn_marker
    if pause_requested(session):
        return "wait"
    try:
        held = is_held(_root(session) / "core-update")
    except OSError:
        held = True
    if held:
        return "wait"
    # Settled first: a turn the user (or another path) already moved on is never handed off or resumed.
    with session["history_lock"]:
        busy = bool(session.get("running") or session.get("_closing") or session.get("_finalized")
                    or session.get("_auto_continue_scheduled"))
    if busy:
        return "done"
    marker = read_turn_marker(_home(session), marker_key)
    if not marker or marker.get("reason") != PAUSE_REASON:
        return "done"
    if (skew or _detect_skew)() is not None:
        return "leave"
    return "resume"


def replacement_outlook() -> str:
    """Whether a successor backend is on its way for a skewed process: ``coming`` (a retirement is
    preparing/committed, or the skew watchdog runs and the idle proof it retires through holds),
    ``blocked`` (the watchdog runs but other work or an unreadable ledger keeps this process up) or
    ``none`` (nothing in this process will retire it)."""
    from hermes_cli.backend_retirement import retirement
    from hermes_cli.web_server_skew_exit import skew_watchdog_armed
    if retirement.retiring():
        return "coming"
    if not skew_watchdog_armed():
        return "none"
    from hermes_cli.web_server_skew_exit import _update_in_progress
    if _update_in_progress():
        return "blocked"  # the watchdog never retires while an update marker or checkout lock is live
    from hermes_cli.web_server_idle_proof import idle_proof
    try:
        idle = idle_proof()["idle"]
    except Exception:
        idle = None
    return "coming" if idle is True else "blocked"


def start_in_place_watcher(sid: str, session: dict, marker_key: str) -> threading.Thread:
    """Bounded daemon: continue the paused turn here when its window ends without an update, or when
    the checkout moved but no successor backend is coming to continue it."""

    def run() -> None:
        from tui_gateway import server
        started, left_at, blocked_since = time.monotonic(), None, None
        while time.monotonic() - started < WATCH_MAX_S:
            time.sleep(WATCH_POLL_S)
            try:
                action = in_place_resume_action(session, marker_key)
                if action == "leave":
                    now = time.monotonic()
                    if left_at is None:
                        left_at = now
                        logger.info("core-update pause: checkout moved; session %s continues on the next backend",
                                    marker_key)
                    outlook = replacement_outlook()
                    if outlook == "coming":
                        blocked_since = None
                    elif blocked_since is None:
                        blocked_since = now
                    blocked_for = 0.0 if blocked_since is None else now - blocked_since
                    if outlook == "none" or blocked_for >= NO_REPLACEMENT_S:
                        logger.info("core-update pause: no backend replacement is coming (%s); continuing session "
                                    "%s here", "no retirement watchdog" if outlook == "none" else
                                    f"backend kept busy for {blocked_for:.0f}s", marker_key)
                    elif now - left_at >= LEAVE_GRACE_S:
                        logger.info("core-update pause: backend still not replaced after %.0fs; continuing session "
                                    "%s here", LEAVE_GRACE_S, marker_key)
                    else:
                        continue
                    action = "resume"
                if action == "wait":
                    continue
                if action == "resume":
                    if left_at is None:
                        logger.info("core-update pause: window ended without an update; continuing session %s "
                                    "here", marker_key)
                    if server._maybe_schedule_auto_continue(sid, session, marker_key) is None:
                        continue  # admission refused or not yet idle: the next poll decides again
                return
            except Exception:
                logger.debug("core-update pause watcher for %s failed a poll", marker_key, exc_info=True)

    thread = threading.Thread(target=run, daemon=True, name=f"update-pause-watch-{sid}")
    thread.start()
    return thread
