"""[daveey] Core-update pause for Desktop/TUI turns (t_01a1827558218fed).

hermes-sync's drain window holds update admission while the turns in flight finish, so one long turn used
to hold an update for the whole window, again and again. Once the window has waited a while and every
remaining turn is one the updated backend auto-continues, the drainer writes a pause request beside the
hold (``hermes_cli.admission_fence.pause_requested``). Each such turn then ends at its next step boundary
(after its tool results are persisted, before the next model call; ``agent.turn_iteration_prep``), keeps
its interrupted-turn marker stamped ``reason: core_update_pause``, and releases its admission reservation,
so the update applies. The next backend's startup sweep continues the turn with ``pause_note`` and no
crash-resume attempt is used. After ``MAX_PAUSES`` checkpoints, a failed window no longer restarts
the turn while its drainer holds ``cache/core-update/drain.lock``: it stays idle for the next window.
The threshold bounds no-progress continuations, never the checkpoint needed to let the update apply.

A turn the next backend would not auto-continue is never paused. A paused turn whose window ends without
an update (expired, apply refused) continues in place (``start_in_place_watcher``), unless its pause
budget is spent and the drainer still runs. Drainer exit/cancellation releases that wait automatically, and
so does ``PARK_MAX_S`` since the pause: a paused turn never waits on a drainer that cannot apply. A turn
paused ``STUCK_WARN_S`` logs a WARNING naming what it waits on.
When the update did move the checkout, the turn is left for a successor only while one is coming: this
process's skew watchdog runs and its idle proof holds (or a retirement is already under way). A backend
kept busy by other work for ``NO_REPLACEMENT_S``, one no watchdog retires, or one that has not begun
retiring ``HANDOFF_WAIT_S`` after the move continues the turn here. Every bound counts from the moment
the move was first seen, so a fluctuating outlook or a synthesized turn running over the pause cannot
restart it. From the end of the window until the continuation starts, automatic turns (kanban notices,
process completions, wake-ups) wait (``continuation_due``): one claiming the idle paused session would
keep the backend busy, block the hand-off and delay the paused turn by its whole run.
The retirement fence makes the two exclusive: in-place work is admitted through it, so once retirement
prepares or commits, the turn is left to the successor's startup sweep.
"""

from __future__ import annotations

import logging
import _thread
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PAUSE_REASON = "core_update_pause"
UPDATE_KILL_REASON = "killed by the Hermes update"
# Restart advice on every job an update or backend exit stopped: only persisted jobs survive both. [daveey]
PERSIST_RESTART_HINT = "restart it with persist_on_release=true so it survives updates"
MAX_PAUSES = 3
PAUSE_STATUS_TEXT = "Paused for a Hermes update; continues automatically"
# The update itself takes a few minutes: a turn that would age out of the freshness window meanwhile is
# not paused (mirrors hermes-sync's AUTO_CONTINUE_UPDATE_MARGIN_S).
UPDATE_MARGIN_S = 600.0
# [daveey] Capability flag hermes-sync reads from this file's text (never imports it): a cooperative pause
# of a live turn restarts the auto-continue freshness clock at ``paused_at`` (turn_marker.freshness_since),
# so ``not_resumable_reason`` no longer rejects a live turn for the age of its ``started_at``.
PAUSE_RESTARTS_FRESHNESS = True
# [daveey] t_01a1c7cf6f171e47: capability flag hermes-sync reads from this file's text (never imports it): a
# turn inside a long tool call no longer holds the update until the call ends. ``MID_TOOL_GRACE_S`` after the
# later of the request's first sighting and the newest running call's start (so no call is cut before its
# own grace), a still-running call of an auto-continuable turn
# is cancelled through the user-Stop interrupt path (``start_mid_tool_cancel``), its result is persisted as
# ``interrupted_tool_result`` and the turn pauses at the step boundary right after it, as ``boundary_pause_hook``
# does. Turns ``not_resumable_reason`` rejects are never cancelled.
MID_TOOL_CANCEL = True
MID_TOOL_GRACE_S = 30.0
MID_TOOL_POLL_S = 1.0
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
# [daveey] t_01a18300e42c3364 (2nd attempt): an idle backend with a live watchdog retires within two skew
# polls (_CONFIRMATIONS x DEFAULT_SKEW_POLL_S = 60 s) of the move being visible. One that has not even begun
# retiring after this long is not going to: continue here instead of trusting the outlook up to the backstop.
HANDOFF_WAIT_S = 90.0
# Automatic turns hold off at most this long past the watcher's last poll, so a dead watcher or a failed
# continuation kickoff never holds them for good.
DUE_HOLD_S = 2 * WATCH_POLL_S + 5.0
# After the watcher schedules the continuation, its kickoff may wait up to 120 s for an agent build
# (session_auto_continue) before it claims the session; automatic turns wait that long at most.
KICKOFF_HOLD_S = 120.0 + DUE_HOLD_S
# Automatic turns never wait for a paused turn longer than this past the window's end (a continuation that
# keeps failing to schedule, e.g. a live foreign marker writer, must not starve the chat's notices).
HOLD_MAX_S = LEAVE_GRACE_S + WATCH_POLL_S
# A capped pause parked between drain windows restamps its freshness clock this often (``_keep_parked``).
PARK_STAMP_S = 30.0
# [daveey] t_01a18b11cabcd2c2: a capped pause used to stay parked for its drainer's whole life (David-MBP
# 2026-10-29: two chats paused 20:24, continued 21:25 because the drainer lived 66 min waiting on a peer app
# build). A paused turn may wait on the apply itself, never on anything else: once its pause is this old and
# the window is over, it continues in place even while the drainer still runs.
PARK_MAX_S = 600.0
# A turn still paused this long logs a WARNING naming what it waits on (once per pause).
STUCK_WARN_S = 900.0


def _now() -> float:
    return time.time()


def _mono() -> float:
    return time.monotonic()


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
    # The pause restarts the freshness clock at ``paused_at`` (PAUSE_RESTARTS_FRESHNESS), so the turn's age
    # no longer matters; only that the window outlasts the update itself.
    if fresh_s <= UPDATE_MARGIN_S:
        return "auto-continue freshness window is shorter than the update margin"
    return None


def _command_head(command: str, limit: int = 80) -> str:
    head = " ".join(str(command or "").split())
    return head if len(head) <= limit else head[:limit - 3] + "..."


def _format_start(started_at: Any) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(started_at)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown"


def _killed_job_notice(receipt: dict) -> str:
    """The normal completion text, with the receipt's reason (default: the update) named."""
    from tools.process_registry_notifications import format_process_notification
    text = format_process_notification({
        "type": "completion", "session_id": receipt["session_id"], "command": receipt.get("command") or "",
        "exit_code": receipt.get("exit_code", -15), "completion_reason": "killed",
        "termination_source": receipt.get("termination_source") or PAUSE_REASON,
        "kill_reason": receipt.get("kill_reason") or UPDATE_KILL_REASON,
        "output": receipt.get("output") or "",
    })
    return text or ""


def _update_jobs(session: dict) -> list[dict]:
    """Snapshot and redact before journaling intent, so no kill can go unmentioned after a crash."""
    from tools.process_registry import process_registry, _redact_process_result
    owners = set(getattr(session.get("agent"), "_process_owner_task_ids", ()) or ())
    jobs = []
    for proc in process_registry.list_sessions():
        if proc.get("owner_task_id") not in owners or proc.get("status") != "running" or proc.get("persist_on_release"):
            continue
        live = process_registry.get(proc["session_id"])
        if live is not None:
            job = _redact_process_result({"session_id": live.id, "command": live.command,
                "started_at": live.started_at, "notify_on_complete": live.notify_on_complete,
                "kill_state": "pending"})
            job["command"] = _command_head(job["command"])
            jobs.append(job)
    return jobs


def stop_owned_jobs_for_update_pause(jobs: list[dict], save: Callable[[], bool], *, source: str = PAUSE_REASON,
                                     reason: str = UPDATE_KILL_REASON) -> bool:
    """Journal intent BEFORE signaling, and each outcome before proceeding. Refuse pause on failure.
    ``source``/``reason`` name who stopped the job (the backend-exit journal passes its own)."""
    from tools.process_registry import process_registry, _redact_process_result
    for job in jobs:
        if job.get("kill_state") in {"killed", "completed"}:
            continue
        sid = job["session_id"]
        if source != PAUSE_REASON:
            job.update(termination_source=source, kill_reason=reason)
        result = process_registry.kill_process(sid, source=source, consume_output=False)
        if result.get("status") == "already_exited":
            job["kill_state"] = "already_exited"
        elif result.get("status") == "killed":
            live = process_registry.get(sid)
            job.update(kill_state="killed", exit_code=getattr(live, "exit_code", -15),
                       output=result.get("output") or "")
            _redact_process_result(job)
            if job.get("notify_on_complete"):
                job["notice"] = _killed_job_notice(job)
        else:
            logger.warning("background job %s not stopped by %s (%s)", sid, source, result.get("status"))
            return False
        if not save():
            return False
    return True


def _drainer_lock(session: dict) -> Path:
    """The v1 request may come from a drainer serving several installation roots."""
    import json
    fallback = _root(session) / "cache" / "core-update" / "drain.lock"
    try:
        body = json.loads((_root(session) / "core-update" / "pause-request").read_text(encoding="utf-8-sig"))
        path = Path(body.get("drain_lock") or fallback)
        return path if path.is_absolute() else fallback
    except (OSError, ValueError, TypeError, AttributeError):
        return fallback


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
        from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker
        home = _home(session)
        marker = read_turn_marker(home, marker_key)
        if marker is None:
            return None
        jobs = {job["session_id"]: job for job in marker.get("killed_jobs", [])}
        jobs.update({job["session_id"]: job for job in _update_jobs(session)})
        killed = list(jobs.values())
        # Write-ahead journal: even a crash during kill or a later disk failure leaves an
        # explicit uncertain-outcome record. Never claim an unverified kill succeeded.
        if not mark_turn_paused(home, marker_key, PAUSE_REASON, killed_jobs=killed,
                                increment=marker.get("reason") != PAUSE_REASON):
            return None
        def save() -> bool:
            return mark_turn_paused(home, marker_key, PAUSE_REASON, killed_jobs=killed, increment=False)
        try:
            if not stop_owned_jobs_for_update_pause(killed, save):
                return None
        except Exception:
            logger.warning("core-update pause: background cleanup failed; refusing pause for %s", marker_key, exc_info=True)
            return None
        drainer_lock = _drainer_lock(session)  # request disappears when the window ends
        with session["history_lock"]:
            session["_update_pause_marker_key"] = marker_key
            session["_update_pause_drainer_lock"] = drainer_lock
            session.pop("_update_pause_parked_stamp", None)  # a new pause starts unparked
        logger.info("core-update pause: session %s paused at a step boundary for a Hermes update", marker_key)
        return PAUSE_REASON

    return hook


def interrupted_tool_result(at: float | None = None) -> str:
    """The persisted result of a tool call the update cancelled (UTC, whole seconds)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now() if at is None else at))
    return f"[interrupted by Hermes update at {stamp}: re-run if still needed]"


def mid_tool_cancel_step(session: dict, marker_key: str, agent: Any, state: dict) -> list[str] | None:
    """One poll of ``start_mid_tool_cancel``: the cancelled call ids once the grace ran out, else None.
    ``state`` carries the request's first sighting (and the last decline logged) between polls."""
    from agent import update_pause_cancel
    if not pause_requested(session):
        state.clear()  # a later request starts its own grace (and may cancel again)
        return None
    if state.get("cancelled"):
        return None  # one cancel per request: the turn pauses at the next boundary
    now = _mono()
    seen = state.setdefault("seen", now)
    running = update_pause_cancel.in_flight(agent)
    if not running or now - max(seen, max(running.values())) < MID_TOOL_GRACE_S:
        return None
    why = not_resumable_reason(session, marker_key)
    if why:
        if state.get("declined") != why:
            state["declined"] = why
            logger.info("core-update pause: session %s keeps its running tool call (%s)", marker_key, why)
        return None
    cancelled = update_pause_cancel.cancel(agent, interrupted_tool_result())
    if cancelled:
        state["cancelled"] = True
        logger.info("core-update pause: session %s cancelled %d running tool call(s) after %.0fs for a Hermes "
                    "update: %s", marker_key, len(cancelled), MID_TOOL_GRACE_S, ", ".join(cancelled))
    return cancelled


def start_mid_tool_cancel(session: dict, marker_key: str, agent: Any) -> Callable[[], None]:
    """Per-turn daemon (one stat per ``MID_TOOL_POLL_S`` while no pause is requested); returns its stop. After
    a cancel the turn pauses at the next step boundary (turn_iteration_prep), which ends the turn and this
    watcher; if the request was withdrawn by then the turn goes on, and a later request may cancel again."""
    stop = threading.Event()

    def run() -> None:
        state: dict = {}
        while not stop.wait(MID_TOOL_POLL_S):
            try:
                mid_tool_cancel_step(session, marker_key, agent, state)
            except Exception:
                logger.debug("core-update mid-tool cancel poll for %s failed", marker_key, exc_info=True)

    # The low-level primitive, not threading.Thread: harnesses that run turn threads inline patch that class,
    # and an inline watcher would block the turn forever. A _thread thread never holds interpreter exit.
    _thread.start_new_thread(run, ())
    return stop.set


# [daveey] t_01a18ac84e3491a9: a turn parked in a blocking clarify never reaches a step boundary, so the
# drainer's pause budget runs out and the update cuts it (hub 2026-10-30 05:37: a clarify waited 41 min, the
# turn crash-resumed). While a pause is requested and the turn's boundary hook would pause it, the clarify is
# withdrawn unanswered; the hook then pauses at the boundary right after it and the continuation re-asks.
CLARIFY_PAUSE_STATUS = "paused_for_update"
CLARIFY_PAUSE_NOTICE = ("Hermes is pausing this turn at a step boundary for an update, so this question was "
                        "withdrawn unanswered. The turn continues automatically after the update: ask it again then.")
_clarify_provider_lock = threading.Lock()
_clarify_provider_installed = False


def _session_for(session_id: str) -> dict | None:
    from tui_gateway import server
    with server._sessions_lock:
        sessions = list(server._sessions.values())
    for session in sessions:
        agent = session.get("agent")
        if session.get("session_key") == session_id or getattr(agent, "session_id", None) == session_id:
            return session
    return None


def clarify_pause_provider(session_id: str, tool_call_id: str):
    """``tools.clarify_early`` provider: withdraw a blocked clarify when this turn will pause for an update."""
    if not session_id:
        return None
    session = _session_for(session_id)
    if session is None or getattr(session.get("agent"), "step_boundary_pause", None) is None:
        return None
    marker_key = str(session.get("session_key") or "")
    if not marker_key or not pause_requested(session) or not_resumable_reason(session, marker_key):
        return None
    from tools.clarify_early import EarlyAnswer
    logger.info("core-update pause: session %s withdrew a pending clarify to pause for a Hermes update", marker_key)
    return EarlyAnswer({"status": CLARIFY_PAUSE_STATUS, "outcome": CLARIFY_PAUSE_STATUS,
                        "notice": CLARIFY_PAUSE_NOTICE}, commit=lambda: None)


def install_clarify_pause_provider() -> None:
    """Idempotent; called when a turn installs its boundary hook."""
    global _clarify_provider_installed
    with _clarify_provider_lock:
        if _clarify_provider_installed:
            return
        from tools import clarify_early
        clarify_early.register_provider(clarify_pause_provider)
        _clarify_provider_installed = True


def pause_note(prompt: str, killed_jobs: list | None = None) -> str:
    from tui_gateway.session_history import _AUTO_CONTINUE_NOTE_PREFIX
    note = (f"{_AUTO_CONTINUE_NOTE_PREFIX} — it was paused at a step boundary for a Hermes update. Every "
            "tool result above is complete; continue with the next step and don't redo finished steps. "
            f"The paused request was:]\n\n{prompt}")
    report = killed_jobs_report(killed_jobs, "Background jobs stopped for this Hermes update:")
    return f"{note}\n\n{report}" if report else note


def killed_jobs_report(killed_jobs: list | None, heading: str) -> str:
    """One line per stopped job (id, UTC start, command head, restart advice) plus, for a notify job,
    its completion notice with the reason. Empty when there are none."""
    jobs = [job for job in (killed_jobs or []) if isinstance(job, dict) and job.get("session_id")]
    if not jobs:
        return ""
    lines = ["Background job results at interruption:" if any(
        job.get("kill_state") == "completed" for job in jobs) else heading]
    for job in jobs:
        command = job.get("command") or "(no command)"
        state = job.get("kill_state", "killed")
        if state == "completed":
            lines.append(job.get("notice") or "")
            continue
        if state != "killed":
            lines.append(f"- {job['session_id']} started {_format_start(job.get('started_at'))}: {command} "
                         f"— interruption outcome {state}; inspect its result before restarting")
            continue
        lines.append(f"- {job['session_id']} started {_format_start(job.get('started_at'))}: {command} "
                     f"— restart it if you still need it; {PERSIST_RESTART_HINT}")
        if job.get("notify_on_complete"):
            notice = job.get("notice") or _killed_job_notice(job)
            if notice:
                lines.append(notice)
    return "\n".join(lines)


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


def drain_retry_pending(session: dict, marker: dict) -> bool:
    """A capped pause stays idle between windows of its live drainer, not in a pause/continue loop.

    Only the original writer defers here: a replacement must recover its dead writer's marker.
    The flock (not file existence or a PID) releases on cancellation/crash. Code skew also
    releases this gate so the existing updated-backend handoff/recovery bounds still apply.
    """
    import os
    if (marker.get("reason") != PAUSE_REASON or marker.get("pauses", 0) < MAX_PAUSES
            or marker.get("writer_pid") != os.getpid() or _detect_skew() is not None):
        return False
    try:
        paused_for = _now() - float(marker.get("paused_at") or marker.get("started_at") or 0.0)
    except (TypeError, ValueError):
        paused_for = PARK_MAX_S
    if paused_for >= PARK_MAX_S:
        return False  # bounded: a parked turn never waits out a drainer that cannot apply
    if os.name != "posix":
        return False  # hermes-sync's POSIX drain protocol is not present
    import fcntl
    try:
        path = session.get("_update_pause_drainer_lock") or _root(session) / "cache" / "core-update" / "drain.lock"
        with Path(path).open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unreadable lock is not evidence that the drainer exited
    return False


def in_place_resume_action(session: dict, marker_key: str, *, skew: Callable[[], Any] | None = None) -> str:
    """What a paused turn's own backend does now: ``wait`` (the window is still up), ``carried`` (the window
    is over but a synthesized turn runs over the pause: wait for it), ``leave`` (the checkout moved: the next
    backend may continue it), ``resume`` (no update replaced us: continue here) or ``done`` (the user or
    another path already moved on)."""
    from hermes_cli.admission_fence import is_held
    from tui_gateway.turn_marker import read_turn_marker

    def why(text: str) -> None:
        session["_update_pause_why"] = text  # read by the watcher's stuck-pause WARNING

    if pause_requested(session):
        why("the drain window is still up (pause requested, admission held)")
        return "wait"
    try:
        held = is_held(_root(session) / "core-update")
    except OSError:
        held = True
    if held:
        why("update admission is held (drain window or update in progress)")
        return "wait"
    # Settled first: a turn the user (or another path) already moved on is never handed off or resumed.
    with session["history_lock"]:
        busy = bool(session.get("running") or session.get("_closing") or session.get("_finalized")
                    or session.get("_auto_continue_scheduled"))
    if busy:
        # A synthesized turn running over the paused marker (prompt_turn._carry_paused_marker) is not the
        # user moving on: wait for it, then continue the paused turn.
        with session["history_lock"]:
            carried = session.get("_paused_marker_carried") == marker_key and session.get("running")
        if carried:
            why("a synthesized turn runs over the paused marker")
        return "carried" if carried else "done"
    marker = read_turn_marker(_home(session), marker_key)
    if not marker or marker.get("reason") != PAUSE_REASON:
        return "done"
    if (skew or _detect_skew)() is not None:
        _keep_parked(session, marker_key, last=True)
        why("the checkout moved; waiting for a replacement backend")
        return "leave"
    if drain_retry_pending(session, marker):
        _keep_parked(session, marker_key)
        why(f"pause budget spent ({marker.get('pauses')} pauses); parked until the drainer applies or "
            f"{PARK_MAX_S:.0f}s pass")
        return "wait"
    why("continuation not yet admitted")
    _keep_parked(session, marker_key, last=True)
    return "resume"


def _keep_parked(session: dict, marker_key: str, *, last: bool = False) -> None:
    """Restamp the parked marker's freshness clock (``turn_marker.freshness_since``) while this writer
    keeps a capped pause idle between drain windows: it may wait out several windows (up to the
    drainer's lifetime), and an age measured from ``started_at`` would then drop it as stale at the
    in-place continue or the next backend's startup sweep. Throttled to ``PARK_STAMP_S`` of wall time;
    ``last`` stamps once more as parking ends (drainer exit, checkout moved) so a wall-clock jump while
    parked (a sleeping laptop) cannot age it out at that edge. Afterwards the usual window applies."""
    from tui_gateway.turn_marker import mark_turn_parked
    with session["history_lock"]:
        prev = session.get("_update_pause_parked_stamp")
        if last:
            if prev is None:
                return  # never parked: a plain pause's clock is paused_at (turn_marker.freshness_since)
            session.pop("_update_pause_parked_stamp", None)
        elif prev is not None and 0 <= _now() - prev < PARK_STAMP_S:
            return
        else:
            session["_update_pause_parked_stamp"] = _now()
    mark_turn_parked(_home(session), marker_key)


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


def continuation_due(session: dict) -> bool:
    """[daveey] True while a paused turn's window is over and its continuation has not started: an automatic
    turn claiming the idle session now would run first and delay the paused turn by its whole run (a kanban
    notice took 8 min on 2026-10-28), and its work would keep the backend from retiring for the hand-off.
    Read live while a watcher runs (a notice can land between two of its polls), then held by the deadline
    until the continuation records its marker."""
    if time.monotonic() < float(session.get("_update_pause_due_until") or 0.0):
        return True
    if time.monotonic() >= float(session.get("_update_pause_watch_until") or 0.0):
        # Keep a surviving backend's bounded hold unchanged. A replacement has neither
        # field: its only ordering evidence is the previous writer's durable marker.
        if session.get("_update_pause_watched") or "_update_pause_due_until" in session:
            return False
        return _durable_continuation_due(session)
    return _window_over(session)


def _durable_continuation_due(session: dict) -> bool:
    from tui_gateway import server
    from tui_gateway.turn_marker import freshness_since, marker_writer_state, read_turn_marker
    key = str(session.get("session_key") or "")
    marker = read_turn_marker(_home(session), key)
    if not marker or marker.get("reason") != PAUSE_REASON or not marker.get("auto_continue", True):
        return False
    if marker_writer_state(marker) == "alive":
        return False  # its own backend's watcher owns the bounded in-place hold
    # Match scheduling eligibility so disabled, stale, or exhausted pauses don't
    # indefinitely starve automatic delivery when the startup reader skips them.
    with server._session_profile_runtime_scope(session):
        enabled, freshness, attempts = server._auto_continue_config()
    if not (enabled and _now() - freshness_since(marker) <= freshness and marker["attempts"] < attempts):
        return False
    # Same cap as the in-place hold (HOLD_MAX_S): a continuation this backend keeps failing to admit
    # (a live foreign owner, a startup sweep that gave up) must not refuse the chat's notices for the
    # whole freshness window. The clock starts when this backend first sees this pause and is keyed by
    # the pause's identity, so re-reading the same marker never re-arms it; only a new pause does.
    identity = (marker.get("started_at"), marker.get("paused_at"), marker.get("pauses"))
    with session["history_lock"]:
        seen = session.get("_update_pause_durable_seen")
        if not seen or seen[0] != identity:
            seen = session["_update_pause_durable_seen"] = (identity, _mono())
    return _mono() - seen[1] < HOLD_MAX_S


def _note_watching(session: dict) -> None:
    """A watcher is alive for this session; lapses by itself if the watcher thread never runs or dies."""
    with session["history_lock"]:
        session["_update_pause_watched"] = True
        session["_update_pause_watch_until"] = time.monotonic() + DUE_HOLD_S


def _window_over(session: dict) -> bool:
    from hermes_cli.admission_fence import is_held
    if pause_requested(session):
        return False
    try:
        return not is_held(_root(session) / "core-update")
    except OSError:
        return False


def _hold_automatic_turns(session: dict, hold_s: float = DUE_HOLD_S) -> None:
    with session["history_lock"]:
        session["_update_pause_due_until"] = time.monotonic() + hold_s


def _release_automatic_turns(session: dict, *, watching: bool = False) -> None:
    with session["history_lock"]:
        if watching:
            session.pop("_update_pause_watch_until", None)
        if not session.get("_auto_continue_scheduled"):  # another watcher's continuation is still starting
            session.pop("_update_pause_due_until", None)


def _leave_decision(waited: float, outlook: str) -> str | None:
    """Why to continue a paused turn here now that the checkout moved (``waited`` s ago), else None."""
    if outlook == "retiring":
        return None  # the fence refuses in-place work from here on; only the backstop retries
    if outlook == "none":
        return "no retirement watchdog"
    if outlook == "blocked" and waited >= NO_REPLACEMENT_S:
        return f"backend kept busy, {waited:.0f}s since the checkout moved"
    if waited >= HANDOFF_WAIT_S:
        return f"no retirement began within {waited:.0f}s of the checkout moving"
    return None


def warn_if_stuck(session: dict, marker_key: str, paused_for: float, warned: bool) -> bool:
    """[daveey] One WARNING per pause once a turn has been paused ``STUCK_WARN_S``, naming the session and
    what it waits on (the last ``in_place_resume_action`` reason). Returns the new ``warned`` flag."""
    if warned or paused_for < STUCK_WARN_S:
        return warned
    logger.warning("core-update pause: session %s has been paused for %.0fs (over %.0fs); blocked by: %s",
                   marker_key, paused_for, STUCK_WARN_S, session.get("_update_pause_why") or "unknown")
    return True


def start_in_place_watcher(sid: str, session: dict, marker_key: str) -> threading.Thread:
    """Bounded daemon: continue the paused turn here when its window ends without an update, or when
    the checkout moved but no successor backend is coming to continue it."""

    def run() -> None:
        from tui_gateway import server
        from hermes_cli.backend_retirement import retirement
        started, left_at, last_outlook, scheduled, over_at = time.monotonic(), None, None, False, None
        warned = False
        try:
            while time.monotonic() - started < WATCH_MAX_S:
                time.sleep(WATCH_POLL_S)
                warned = warn_if_stuck(session, marker_key, time.monotonic() - started, warned)
                # Automatic turns wait for the paused turn at most HOLD_MAX_S past the window's end: a
                # continuation that keeps failing to schedule must not starve the chat's notices for hours.
                holding = over_at is None or time.monotonic() - over_at < HOLD_MAX_S
                if holding:
                    _note_watching(session)
                else:
                    _release_automatic_turns(session, watching=True)
                try:
                    action = in_place_resume_action(session, marker_key)
                    if action == "wait" and over_at is None and _window_over(session):
                        # A capped pause may wait across windows, but notices still keep their bounded hold.
                        over_at = time.monotonic()
                    if action in ("leave", "resume", "carried"):
                        if over_at is None:
                            over_at = time.monotonic()
                        if holding:
                            _hold_automatic_turns(session)  # the window is over: the paused turn goes first
                    if action == "carried" and left_at is None and _detect_skew() is not None:
                        left_at = time.monotonic()  # the hand-off clock starts at the move, not after this turn
                        logger.info("core-update pause: checkout moved while a synthesized turn runs over session "
                                    "%s; the paused turn continues after it", marker_key)
                    if action in ("wait", "carried"):
                        continue
                    if action == "leave":
                        now = time.monotonic()
                        if left_at is None:
                            left_at = now
                            logger.info("core-update pause: checkout moved; session %s continues on the next "
                                        "backend if one is coming", marker_key)
                        outlook = "retiring" if retirement.retiring() else replacement_outlook()
                        if outlook != last_outlook:
                            logger.info("core-update pause: replacement outlook for session %s is %s (%.0fs since "
                                        "the checkout moved)", marker_key, outlook, now - left_at)
                            last_outlook = outlook
                        why = _leave_decision(now - left_at, outlook)
                        if why is not None:
                            logger.info("core-update pause: no backend replacement is coming (%s); continuing "
                                        "session %s here", why, marker_key)
                        elif now - left_at >= LEAVE_GRACE_S:
                            logger.info("core-update pause: backend still not replaced after %.0fs; continuing "
                                        "session %s here", LEAVE_GRACE_S, marker_key)
                        else:
                            continue
                        action = "resume"
                    if action == "resume":
                        if left_at is None:
                            logger.info("core-update pause: window ended without an update; continuing session %s "
                                        "here", marker_key)
                        if server._maybe_schedule_auto_continue(sid, session, marker_key) is None:
                            continue  # admission refused or not yet idle: the next poll decides again
                        scheduled = True
                        # The kickoff may wait for an agent build before it claims the session: hold until
                        # the continuation records its marker (prompt_turn pops this), bounded by the build.
                        _hold_automatic_turns(session, KICKOFF_HOLD_S)
                    return
                except Exception:
                    logger.debug("core-update pause watcher for %s failed a poll", marker_key, exc_info=True)
        finally:
            if not scheduled:
                _release_automatic_turns(session, watching=True)
            else:
                with session["history_lock"]:
                    session.pop("_update_pause_watch_until", None)

    thread = threading.Thread(target=run, daemon=True, name=f"update-pause-watch-{sid}")
    _note_watching(session)  # before start: automatic turns see the watcher from its first instant
    thread.start()
    return thread
