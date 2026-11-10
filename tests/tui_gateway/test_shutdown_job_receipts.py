"""A backend shutdown must not silently drop an idle chat's notify jobs.

The common CI-wait workflow starts ``terminal(background=true, notify=true)`` and ends the turn. When a
Hermes update (or any backend restart) retires that backend, no turn is in flight, so the step-boundary
pause never runs: the process exits through ``_shutdown_sessions`` → ``agent.close`` →
``kill_processes(source="agent_close", consume_output=True)`` and the completion is consumed.

Contract tested here: before sessions are closed at backend exit, every session's owned non-persist
running jobs are journaled into the durable turn marker (write-ahead), stopped with source
``backend_shutdown`` and ``consume_output=False``, and the outcome is journaled. An idle session gets a
marker only when one of those jobs was ``notify=true`` (its notice would have woken the chat); the next
backend's auto-continue then submits a turn listing each job (id, UTC start, command head, restart
advice) and the normal completion notice with the reason. A turn already in flight keeps its crash
marker and gains the same list. ``persist_on_release`` jobs are untouched; a real session end (not
backend exit) keeps today's behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start
from tools.process_registry import ProcessRegistry, ProcessSession

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True,
                                     "freshness_minutes": 15, "max_attempts": 2}}}
STARTED = 1_700_000_000.0
UPDATE_REASON = "killed by the Hermes update"
SHUTDOWN_REASON = "killed when the Hermes backend shut down"


class _LiveProc:
    def __init__(self, pid):
        self.pid = pid
        self._alive = True
        self.stdout = self.stderr = self.stdin = None

    def poll(self):
        return None if self._alive else 0


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


_pids = iter(range(5000, 6000))


def _job(sid, *, owner, command, notify=False, persist=False, session_key="session-key"):
    job = ProcessSession(id=sid, command=command, task_id=owner, owner_task_id=owner,
                         session_key=session_key, started_at=STARTED)
    job.notify_on_complete = notify
    job.persist_on_release = persist
    job.process = _LiveProc(next(_pids))
    return job


def _session(home, owner="turn-1", **extra):
    agent = types.SimpleNamespace(_process_owner_task_ids=(owner,))
    return {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
            "history_version": 0, "running": False, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
            "profile_home": str(home), **extra}


def _start(epoch=STARTED) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 15 * 60.0, 2), raising=False)
    monkeypatch.setattr("hermes_cli.update_lock.update_in_progress", lambda *a, **k: False)
    monkeypatch.setattr(up, "_detect_skew", lambda: ("old", "new"))
    return tmp_path


@pytest.fixture()
def registry(monkeypatch):
    reg = ProcessRegistry()

    def _stop(pid, expected_start=None):
        for job in (*reg._running.values(), *reg._finished.values()):
            proc = job.process
            if proc is not None and getattr(proc, "pid", None) == pid:
                proc._alive = False

    monkeypatch.setattr(reg, "_terminate_host_pid", _stop)
    monkeypatch.setattr(reg, "_write_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr("tools.process_registry.save_completed_result", lambda *a, **k: None)
    monkeypatch.setattr("tools.process_registry.process_registry", reg)
    return reg


@pytest.fixture()
def backend(monkeypatch, registry):
    """The exit path with the real agent-close kill: only the unrelated exit steps are stubbed."""
    from agent.client_lifecycle import ClientLifecycleMixin
    import run_agent as ra
    import tools.computer_use.tool as cu

    monkeypatch.setattr(ra, "cleanup_vm", lambda *a, **k: None)
    monkeypatch.setattr(ra, "cleanup_browser", lambda *a, **k: None)
    monkeypatch.setattr(cu, "release_computer_use_session", lambda *a, **k: None)
    sessions: dict = {}
    closed: list = []

    def close(sid, *, end_reason="tui_close", predicate=None):
        session = sessions.pop(sid, None)
        if session is None:
            return False
        closed.append((sid, end_reason))
        for owner in session["agent"]._process_owner_task_ids:
            ClientLifecycleMixin._close_task_resources(session["agent"], owner)
        return True

    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_close_session_by_id", close)
    real = {}
    for name in ("_flush_sessions_before_exit", "_release_gateway_wake_owner", "_stop_turns_before_exit"):
        real[name] = getattr(server, name)
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    return types.SimpleNamespace(sessions=sessions, closed=closed, close=close, real=real)


def _resume_text(monkeypatch, home) -> str:
    submitted = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: submitted.append(text))
    fresh = _session(home)
    assert server._maybe_schedule_auto_continue("sid", fresh, "session-key") is not None
    (text,) = submitted
    return text


def test_idle_shutdown_for_update_journals_notify_job_and_next_backend_reports_it(home, registry, backend,
                                                                                  monkeypatch):
    notify = _job("proc_notify", owner="turn-1", command="gh run watch 123 && ./deploy.sh", notify=True)
    quiet = _job("proc_quiet", owner="turn-1", command="npm run dev")
    persisted = _job("proc_persist", owner="turn-1", command="sleep 9999", notify=True, persist=True)
    for job in (notify, quiet, persisted):
        registry._running[job.id] = job
    backend.sessions["sid"] = _session(home)

    server._shutdown_sessions()

    assert backend.closed == [("sid", "tui_shutdown")]
    assert notify.exited and notify.termination_source == "backend_shutdown"
    assert quiet.exited and quiet.termination_source == "backend_shutdown"
    assert notify.id not in registry._completion_consumed
    assert persisted.exited is False

    marker = read_turn_marker(home, "session-key")
    assert marker is not None, "idle session's notify job was killed with no durable receipt"
    assert [j["session_id"] for j in marker["killed_jobs"]] == ["proc_notify", "proc_quiet"]
    assert all(j["kill_state"] == "killed" for j in marker["killed_jobs"])

    registry.completion_queue.queue.clear()  # the next backend has no in-memory queue
    text = _resume_text(monkeypatch, home)
    assert "paused at a step boundary" not in text
    for sid, command in (("proc_notify", "gh run watch 123 && ./deploy.sh"), ("proc_quiet", "npm run dev")):
        assert sid in text and command in text
        assert "restart it if you still need it" in text
    assert _start() in text
    assert UPDATE_REASON in text
    assert "Background process proc_notify" in text or "proc_notify" in text
    assert "proc_persist" not in text


def test_idle_shutdown_without_update_names_the_backend_shutdown(home, registry, backend, monkeypatch):
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    notify = _job("proc_notify", owner="turn-1", command="sleep 600", notify=True)
    registry._running[notify.id] = notify
    backend.sessions["sid"] = _session(home)

    server._shutdown_sessions()

    assert notify.termination_source == "backend_shutdown"
    text = _resume_text(monkeypatch, home)
    assert SHUTDOWN_REASON in text and UPDATE_REASON not in text
    assert "proc_notify" in text and "sleep 600" in text


def test_shutdown_receipt_for_killed_notify_job_says_how_to_survive_updates(home, registry, backend, monkeypatch):
    """A chat whose CI wait died with the backend must learn how to keep the next one alive."""
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    notify = _job("proc_notify", owner="turn-1", command="hermes-core-wt ship my-fix", notify=True)
    registry._running[notify.id] = notify
    backend.sessions["sid"] = _session(home)

    server._shutdown_sessions()

    assert notify.termination_source == "backend_shutdown" and notify.persist_on_release is False
    text = _resume_text(monkeypatch, home)
    assert "proc_notify" in text
    assert "restart it with persist_on_release=true so it survives updates" in text


def test_idle_session_with_only_quiet_jobs_gets_no_wake_marker(home, registry, backend):
    quiet = _job("proc_quiet", owner="turn-1", command="npm run dev")
    registry._running[quiet.id] = quiet
    backend.sessions["sid"] = _session(home)

    server._shutdown_sessions()

    assert quiet.exited is True
    assert read_turn_marker(home, "session-key") is None


def test_running_turn_at_shutdown_keeps_its_crash_marker_and_lists_the_jobs(home, registry, backend, monkeypatch):
    notify = _job("proc_notify", owner="turn-1", command="sleep 600", notify=True)
    registry._running[notify.id] = notify
    record_turn_start(home, "session-key", "ship the release")
    backend.sessions["sid"] = _session(home, running=True, _exit_keeps_turn_marker=True)

    server._shutdown_sessions()

    marker = read_turn_marker(home, "session-key")
    assert marker["prompt"] == "ship the release" and marker.get("reason") is None
    assert [j["session_id"] for j in marker["killed_jobs"]] == ["proc_notify"]
    text = _resume_text(monkeypatch, home)
    assert "ship the release" in text and "proc_notify" in text and UPDATE_REASON in text
    assert "paused at a step boundary" not in text


def test_persist_on_release_job_is_untouched_by_shutdown_journal(home, registry, backend):
    persisted = _job("proc_persist", owner="turn-1", command="sleep 9999", notify=True, persist=True)
    registry._running[persisted.id] = persisted
    backend.sessions["sid"] = _session(home)

    server._shutdown_sessions()

    assert persisted.exited is False
    assert read_turn_marker(home, "session-key") is None


def test_session_end_close_keeps_todays_silent_kill(home, registry, backend):
    """A real session end (/new, archive, close) is not a backend exit: no journal, agent_close kill."""
    notify = _job("proc_notify", owner="turn-1", command="sleep 600", notify=True)
    registry._running[notify.id] = notify
    backend.sessions["sid"] = _session(home)

    backend.close("sid", end_reason="tui_close")

    assert notify.termination_source == "agent_close"
    assert notify.id in registry._completion_consumed
    assert read_turn_marker(home, "session-key") is None


def test_sigterm_flush_handler_journals_before_chaining(home, registry, backend, monkeypatch):
    """The SIGTERM path journals before a supervisor's SIGKILL can cut the graceful exit short."""
    import signal

    notify = _job("proc_notify", owner="turn-1", command="sleep 600", notify=True)
    registry._running[notify.id] = notify
    backend.sessions["sid"] = _session(home)
    chained = []
    # Production installs server's rebound copy (install_exit_flush_signal_handlers runs on server).
    monkeypatch.setitem(server._exit_flush_prev_handlers, signal.SIGTERM, lambda s, f: chained.append(s))

    server._handle_exit_flush_signal(signal.SIGTERM, None)

    assert chained == [signal.SIGTERM]
    assert notify.termination_source == "backend_shutdown"
    marker = read_turn_marker(home, "session-key")
    assert marker is not None and [j["session_id"] for j in marker["killed_jobs"]] == ["proc_notify"]


@pytest.mark.parametrize("pending", ["refused", "held", "queue", "registry_park", "notification_park"])
def test_finished_notice_survives_orderly_restart(home, registry, backend, monkeypatch, pending):
    from tools.process_registry_notifications import format_process_notification
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit

    job = _job("proc_success", owner="turn-1", command="gh run watch 123", notify=True)
    registry._running[job.id] = job
    job.append_output("CI passed: release verified\n")
    registry._finish_exited(job, 0)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    if pending == "registry_park":
        registry.park_unowned_event(registry.completion_queue.get_nowait())
    elif pending == "notification_park":
        from tui_gateway import notification_park
        monkeypatch.setattr(notification_park, "_parked", [])
        assert notification_park.park(registry.completion_queue.get_nowait())
    elif pending == "held":
        session["_notif_held"].append(registry.completion_queue.get_nowait())
    elif pending == "refused":
        event = registry.completion_queue.get_nowait()
        monkeypatch.setattr(server, "_notif_claim_turn", lambda s: True)
        monkeypatch.setattr(server, "_notif_submit", lambda *a, **kw: False)
        monkeypatch.setattr(server, "_notif_refusal_backoff", lambda s: None)
        server._notif_dispatch_completions("sid", session, [(event, format_process_notification(event))],
                                            registry, None)
        assert registry.completion_queue.qsize() == 1
    # Repeated SIGTERM/atexit journaling and duplicate queue copies must stay one receipt.
    journal_background_jobs_before_exit()
    server._shutdown_sessions()
    marker = read_turn_marker(home, "session-key")
    assert marker is not None, "finished but undelivered completion lost at restart"
    assert len(marker["killed_jobs"]) == 1
    assert marker["killed_jobs"][0]["exit_code"] == 0
    registry.completion_queue.queue.clear()
    monkeypatch.setattr("tools.process_registry.process_registry", ProcessRegistry())
    text = _resume_text(monkeypatch, home)
    assert text.count("CI passed: release verified") == 1
    assert "proc_success" in text
    assert "killed" not in text and "restart it" not in text
    assert "were running" not in text and "jobs stopped" not in text.lower()


@pytest.mark.parametrize("control", ["stop", "quiet", "foreign_session", "consumed", "delivered"])
def test_finished_notice_shutdown_controls(home, registry, backend, monkeypatch, control):
    from tools.process_registry_notifications import format_process_notification

    job = _job("proc_done", owner="turn-1", command="true", notify=True)
    registry._running[job.id] = job
    registry._finish_exited(job, 0)
    event = registry.completion_queue.get_nowait()
    session = backend.sessions["sid"] = _session(home)

    def delivered():
        monkeypatch.setattr(server, "_notif_claim_turn", lambda s: True)
        monkeypatch.setattr(server, "_notif_submit", lambda *a, **kw: True)
        server._notif_dispatch_completions("sid", session, [(event, format_process_notification(event))], registry, None)

    {"stop": lambda: session.__setitem__("_turn_cancel_requested", True),
     "quiet": lambda: monkeypatch.setattr(
         server, "_load_cfg", lambda: {**CFG, "display": {"background_process_notifications": "off"}}),
     "foreign_session": lambda: event.__setitem__("session_key", "other-chat"),
     "consumed": lambda: registry._completion_consumed.add(job.id),
     "delivered": delivered}[control]()
    # A stale duplicate must not resurrect an acknowledged or suppressed completion.
    registry.completion_queue.put(event)
    server._shutdown_sessions()
    assert read_turn_marker(home, "session-key") is None
    assert read_turn_marker(home / "other-profile", "session-key") is None


def test_finished_notice_dedups_held_and_shared_queue(home, registry, backend):
    job = _job("proc_done", owner="turn-1", command="true", notify=True)
    registry._running[job.id] = job
    registry._finish_exited(job, 7)
    event = registry.completion_queue.get_nowait()
    backend.sessions["sid"] = _session(home, _notif_held=[event, dict(event)])
    registry.completion_queue.put(dict(event))
    server._shutdown_sessions()
    receipts = read_turn_marker(home, "session-key")["killed_jobs"]
    assert len(receipts) == 1 and receipts[0]["exit_code"] == 7


def test_shutdown_receipt_fences_later_live_delivery(home, registry, backend):
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    job = _job("proc_done", owner="turn-1", command="true", notify=True)
    registry._running[job.id] = job
    registry._finish_exited(job, 0)
    session = backend.sessions["sid"] = _session(home)
    journal_background_jobs_before_exit()
    assert server._notif_claim_turn(session) is False
    assert read_turn_marker(home, "session-key") is not None


def test_repeated_shutdown_journal_keeps_running_job_kill_receipt(home, registry, backend):
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    job = _job("proc_running", owner="turn-1", command="sleep 600", notify=True)
    registry._running[job.id] = job
    backend.sessions["sid"] = _session(home)
    journal_background_jobs_before_exit()
    server._shutdown_sessions()
    marker = read_turn_marker(home, "session-key")
    assert marker["killed_jobs"][0]["kill_state"] == "killed"


def test_poller_requeue_after_snapshot_is_journaled_from_custody(home, registry, backend, monkeypatch):
    """A poller holding a dequeued event (in-flight custody) hands it back only AFTER the snapshot: the
    journal reads it from custody instead of waiting for the poller to settle."""
    from tools.process_registry_awaiting import release, take_ready
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    job = _job("proc_done", owner="turn-1", command="true", notify=True)
    registry._running[job.id] = job
    registry._finish_exited(job, 0)
    stop, journaled = threading.Event(), threading.Event()
    backend.sessions["sid"] = _session(home, _notif_stop=stop)
    taken = []

    def poller():
        taken.extend(take_ready(registry.completion_queue, 0))
        try:
            assert stop.wait(5) and journaled.wait(5)  # still routing well past any settle budget
            for evt in taken:
                registry.completion_queue.put(evt)
        finally:
            release(taken)

    joins = []

    class _Poller(threading.Thread):
        def join(self, timeout=None):
            joins.append(timeout)
            return super().join(timeout)

    thread = _Poller(target=poller)
    monkeypatch.setattr(server, "_notification_pollers", [(stop, thread)])
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not taken and time.monotonic() < deadline:
            time.sleep(0.01)
        assert taken and registry.completion_queue.qsize() == 0
        journal_background_jobs_before_exit()
        assert joins == [], "the exit journal waited on a poller instead of reading its custody"
        receipts = read_turn_marker(home, "session-key")["killed_jobs"]
        assert [(j["session_id"], j["kill_state"]) for j in receipts] == [("proc_done", "completed")]
    finally:
        journaled.set()
        stop.set()
        thread.join(5)


def _finished_event(registry, sid="proc_done", exit_code=0, **job_kw):
    job = _job(sid, owner="turn-1", command="gh run watch 9", notify=True, **job_kw)
    registry._running[job.id] = job
    job.append_output("CI passed\n")
    registry._finish_exited(job, exit_code)
    return job, registry.completion_queue.get_nowait()


@pytest.mark.parametrize("stopped_by", ["backend_exit", "user_stop"])
def test_backend_exit_interrupt_is_not_a_user_stop(home, registry, backend, stopped_by):
    """The exit's own interrupt sets _turn_cancel_requested (reason backend_exit); only a prior user Stop suppresses."""
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    _, event = _finished_event(registry)
    session = backend.sessions["sid"] = _session(home, running=True, _notif_held=[event])
    if stopped_by == "user_stop":
        session["_turn_cancel_requested"] = True  # Stop pressed; the turn is still settling
    backend.real["_stop_turns_before_exit"](budget_s=0)
    assert session["_turn_cancel_requested"] is True  # both paths now look cancelled to the live flag
    journal_background_jobs_before_exit()
    marker = read_turn_marker(home, "session-key")
    if stopped_by == "user_stop":
        assert marker is None
    else:
        assert marker is not None, "backend_exit interrupt suppressed a held completion"
        assert [(j["session_id"], j["kill_state"], j["exit_code"]) for j in marker["killed_jobs"]] == [
            ("proc_done", "completed", 0)]


def test_in_flight_event_is_journaled_from_registry_custody(home, registry, backend, monkeypatch):
    """A poller dequeued the event and is still routing it: neither queued nor held, yet still owed."""
    from tools.process_registry_awaiting import release, take_ready
    from tools.process_registry_notifications import format_process_notification
    _, event = _finished_event(registry, exit_code=3)
    registry.completion_queue.put(event)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    ready = take_ready(registry.completion_queue, 0)  # the poller's dequeue + in-flight hold
    try:
        assert ready == [event] and registry.completion_queue.qsize() == 0
        server._shutdown_sessions()
        receipts = read_turn_marker(home, "session-key")["killed_jobs"]
        assert [(j["session_id"], j["kill_state"], j["exit_code"]) for j in receipts] == [("proc_done", "completed", 3)]
        # The poller resumes routing after the snapshot: the exit fence keeps it from also delivering it live.
        submitted = []
        monkeypatch.setattr(server, "_notif_submit", lambda *a, **kw: submitted.append(a) or True)
        server._notif_dispatch_completions("sid", session, [(event, format_process_notification(event))],
                                            registry, None)
        assert submitted == []
    finally:
        release(ready)
    from tools import process_registry_awaiting as awaiting
    assert not any(e is event for e in awaiting._in_flight_events)  # release ends the custody record


def test_claimed_but_unpublished_completion_is_journaled_without_waiting(home, registry, backend, monkeypatch):
    """The reader claimed the finish (left _running, result persisted) and is held before publishing the
    completion, well past any settle budget: the journal records the registry's own final event, and the
    later live publication is fenced, so the next backend reports it exactly once."""
    job = _job("proc_done", owner="turn-1", command="gh run watch 9", notify=True)
    registry._running[job.id] = job
    job.append_output("CI passed\n")
    claimed, go = threading.Event(), threading.Event()
    real_release = registry._release_finished_handles

    def held_release(session):
        claimed.set()
        assert go.wait(10)
        return real_release(session)

    monkeypatch.setattr(registry, "_release_finished_handles", held_release)
    reader = threading.Thread(target=registry._finish_exited, args=(job, 0))
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    reader.start()
    try:
        assert claimed.wait(5)
        assert job.id not in registry._running and not job._completion_event.is_set()
        assert registry.completion_queue.qsize() == 0
        server._shutdown_sessions()  # publication stays held until after the whole exit journal
        marker = read_turn_marker(home, "session-key")
        assert marker is not None, "claimed-but-unpublished completion lost at orderly exit"
        receipts = marker["killed_jobs"]
        assert [(j["session_id"], j["kill_state"], j["exit_code"]) for j in receipts] == [("proc_done", "completed", 0)]
        assert "CI passed" in receipts[0]["notice"]
    finally:
        go.set()
        reader.join(5)
    published = registry.completion_queue.get_nowait()
    assert published["session_id"] == "proc_done" and "CI passed" in published["output"]
    assert server._notif_claim_turn(session) is False  # the fence keeps this backend from also delivering it
    monkeypatch.setattr("tools.process_registry.process_registry", ProcessRegistry())
    text = _resume_text(monkeypatch, home)
    assert text.count("CI passed") == 1 and "restart it" not in text


def test_claim_snapshot_never_redacts_under_the_registry_lock(registry):
    """Redaction may look the session up (``process_registry.get`` takes ``registry._lock``) and runs hooks: the
    claim snapshots the event unredacted and publication redacts a copy outside the lock."""
    job = _job("proc_secret", owner="", command="echo sk-ant-api03-" + "x" * 40, notify=True)
    registry._running[job.id] = job
    job.append_output("token sk-ant-api03-" + "y" * 40 + "\n")
    finisher = threading.Thread(target=registry._finish_exited, args=(job, 0), daemon=True)
    finisher.start()
    finisher.join(10)
    assert not finisher.is_alive(), "claim deadlocked on registry._lock"
    published = registry.completion_queue.get_nowait()
    assert published["session_id"] == "proc_secret" and published is not job._completion_notice
    assert job._completion_notice["output"].startswith("token sk-ant-api03-")  # the claim's raw snapshot


def _slow_notice_turn(monkeypatch, *, block_in, start=None):
    """Drive the real poller dispatch → _notif_submit → admission → start chain; ``block_in`` names where it
    stalls: ``slot`` (before admission, like a slow ownership claim), ``admitted`` (after admission returned,
    before the turn's start commit) or None. ``start`` replaces the worker start (``_start_session_work``):
    returns non-None = started (no model turn runs), None = refused, raises = failed; calls are recorded."""
    entered, go = threading.Event(), threading.Event()
    outcomes, texts, starts = [], [], []
    real_admit = server._admit_prompt_turn

    def slot(sid, session):
        if block_in == "slot":
            entered.set()
            assert go.wait(10)

    def admit(*args, **kwargs):
        admitted = real_admit(*args, **kwargs)
        outcomes.append(admitted is not None)
        texts.append(args[2])
        if block_in == "admitted" and admitted is not None:
            entered.set()
            assert go.wait(10)
        return admitted  # on to the real start commit in _run_admitted_prompt

    def start_work(target, *, name, session=None, **kw):
        starts.append(name)
        return (start or (lambda: object()))()

    def prepare_work(target, *, name, session=None, **kw):
        # The prompt turn retains admission before _sessions_lock and spawns under it (``PreparedWork``).
        return types.SimpleNamespace(start=lambda **_k: start_work(target, name=name, session=session),
                                     cancel=lambda: None, abort=lambda: None)

    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: True)
    monkeypatch.setattr(server, "_session_moved_refusal", lambda session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", slot)
    monkeypatch.setattr(server, "_admit_prompt_turn", admit)
    real_start, real_prepare = server._start_session_work, server._prepare_session_work
    monkeypatch.setattr(server, "_start_session_work", start_work)
    monkeypatch.setattr(server, "_prepare_session_work", prepare_work)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_notif_refusal_backoff", lambda s: None)
    return types.SimpleNamespace(entered=entered, go=go, outcomes=outcomes, texts=texts, starts=starts,
                                 real_start=real_start, real_prepare=real_prepare)


def _closing_backend(monkeypatch, backend):
    """Like ``_pop_session_by_id``: close marks the record ``_closing`` before tearing it down."""
    def close(sid, **kw):
        if (session := backend.sessions.get(sid)) is not None:
            session["_closing"] = True
        return backend.close(sid, **kw)
    monkeypatch.setattr(server, "_close_session_by_id", close)


def _next_backend_text(monkeypatch, home, turn) -> str:
    monkeypatch.setattr(server, "_start_session_work", turn.real_start)  # the next backend's own start
    monkeypatch.setattr(server, "_prepare_session_work", turn.real_prepare)
    monkeypatch.setattr("tools.process_registry.process_registry", ProcessRegistry())
    return _resume_text(monkeypatch, home)


def _assert_one_completed_receipt(home, monkeypatch, turn, *, exit_code=0):
    marker = read_turn_marker(home, "session-key")
    assert marker is not None, "pending notice lost at orderly exit"
    assert [(j["session_id"], j["kill_state"], j["exit_code"]) for j in marker["killed_jobs"]] == [
        ("proc_done", "completed", exit_code)]
    assert _next_backend_text(monkeypatch, home, turn).count("CI passed") == 1


@pytest.mark.parametrize("exit_path", ["journal", "full_exit"])
@pytest.mark.parametrize("block_in", ["slot", "admitted"])
def test_notice_turn_claimed_before_exit_fence_but_not_started_is_journaled(home, registry, backend, monkeypatch,
                                                                             block_in, exit_path):
    """A poller claimed the session (running=True) before the exit fence and stalls inside _notif_submit, before
    admission (``slot``) or after it (``admitted``). Admission commits nothing: the worker start is the commit
    point, and it is refused after the fence (``journal``) or the close (``full_exit``: the real
    _shutdown_sessions, whose close sets _closing). The notice is journaled once, never marked delivered, never
    started; the poller's later requeue happens after the last journal and loses nothing."""
    from tools.process_registry_awaiting import release, take_ready
    from tools.process_registry_notifications import format_process_notification
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    _, event = _finished_event(registry, exit_code=0)
    registry.completion_queue.put(event)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    _closing_backend(monkeypatch, backend)
    turn = _slow_notice_turn(monkeypatch, block_in=block_in)

    def poller():
        ready = take_ready(registry.completion_queue, 0)
        try:
            server._notif_dispatch_completions("sid", session, [(e, format_process_notification(e)) for e in ready],
                                                registry, None)
        finally:
            release(ready)

    thread = threading.Thread(target=poller)
    thread.start()
    try:
        assert turn.entered.wait(5) and session["running"] is True
        (journal_background_jobs_before_exit if exit_path == "journal" else server._shutdown_sessions)()
    finally:
        turn.go.set()
        thread.join(5)
    assert not thread.is_alive()
    assert turn.outcomes == [block_in == "admitted"]
    assert turn.starts == [], "a notice turn started after the exit fence"
    assert session.get("_completion_delivered", set()) == set()
    assert not session.get(COMPLETION_UNSECURED)
    assert session["running"] is False
    assert registry.completion_queue.qsize() == 1  # handed back to custody after the journal, not delivered
    server._shutdown_sessions()  # atexit re-journal stays one receipt
    registry.completion_queue.queue.clear()
    _assert_one_completed_receipt(home, monkeypatch, turn)


@pytest.mark.parametrize("start", ["refused", "raises"])
def test_notice_turn_start_refused_or_raising_is_not_delivered(home, registry, backend, monkeypatch, start):
    """No exit in progress: a start that is refused or raises rolls back the delivered mark it made before the
    start, the event goes back to the queue, and a later orderly exit journals it."""
    from tools.process_registry_notifications import format_process_notification
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    _, event = _finished_event(registry, exit_code=4)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])

    def raising():
        raise RuntimeError("thread start failed")

    turn = _slow_notice_turn(monkeypatch, block_in=None, start=(lambda: None) if start == "refused" else raising)
    server._notif_dispatch_completions("sid", session, [(event, format_process_notification(event))], registry, None)
    assert turn.outcomes == [True] and len(turn.starts) == 1
    assert session.get("_completion_delivered", set()) == set()
    assert not session.get(COMPLETION_UNSECURED)
    assert session["running"] is False
    assert [e["session_id"] for e in registry.completion_queue.queue] == ["proc_done"]
    server._shutdown_sessions()
    registry.completion_queue.queue.clear()
    _assert_one_completed_receipt(home, monkeypatch, turn, exit_code=4)


@pytest.mark.parametrize("phase", ["before_marker", "after_marker", "exit_interrupt_before_marker", "user_stop"])
def test_started_notice_turn_keeps_custody_until_its_crash_marker(home, registry, backend, monkeypatch, phase):
    """A notice turn whose start committed before the fence owns the notice (delivered), but until its crash
    marker is on disk the exit journal still reports it (unsecured). The marker step here is the real
    ``_record_turn_marker`` the turn body runs first, called inline (no model turn runs). A backend-exit
    interrupt racing that write keeps the marker; a user Stop before the exit still suppresses both."""
    from tools.process_registry_notifications import format_process_notification
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    _, event = _finished_event(registry, exit_code=0)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    turn = _slow_notice_turn(monkeypatch, block_in=None)
    server._notif_dispatch_completions("sid", session, [(event, format_process_notification(event))], registry, None)
    assert turn.outcomes == [True] and len(turn.starts) == 1
    assert session["_completion_delivered"] == {"proc_done"}
    assert set(session[COMPLETION_UNSECURED]) == {"proc_done"}
    assert registry.completion_queue.qsize() == 0 and session["running"] is True
    (text,) = turn.texts
    if phase == "after_marker":
        assert server._record_turn_marker(session, text) == "session-key"
        assert not session.get(COMPLETION_UNSECURED)
    elif phase in {"exit_interrupt_before_marker", "user_stop"}:
        if phase == "user_stop":
            session["_turn_cancel_requested"] = True
        backend.real["_stop_turns_before_exit"](budget_s=0)  # fence + the exit's own interrupt
        assert session["_turn_cancel_requested"] is True
        server._record_turn_marker(session, text)  # the turn body reaches its marker after the interrupt
    journal_background_jobs_before_exit()
    server._shutdown_sessions()
    marker = read_turn_marker(home, "session-key")
    if phase == "user_stop":
        assert marker is None
        return
    assert marker is not None, "a started notice turn's notice lost at orderly exit"
    if phase == "before_marker":
        assert [(j["session_id"], j["kill_state"]) for j in marker["killed_jobs"]] == [("proc_done", "completed")]
    else:  # the turn's own marker carries the notice; the journal adds no second copy
        assert "CI passed" in marker["prompt"] and not marker.get("killed_jobs")
    assert _next_backend_text(monkeypatch, home, turn).count("CI passed") == 1


def test_held_events_taken_for_routing_stay_in_custody(home, registry, backend):
    """The poller took its held list to route it (out of _notif_held, never in the queue): still owed."""
    from tools.process_registry_awaiting import release
    _, event = _finished_event(registry, exit_code=5)
    session = backend.sessions["sid"] = _session(home, _notif_held=[event])
    taken = server._notif_take_held(session)
    try:
        assert taken == [event] and session["_notif_held"] == []
        server._shutdown_sessions()
        receipts = read_turn_marker(home, "session-key")["killed_jobs"]
        assert [(j["session_id"], j["exit_code"]) for j in receipts] == [("proc_done", 5)]
    finally:
        release(taken)


@pytest.mark.parametrize("park", ["registry", "notification"])
def test_claimed_parked_notice_keeps_exit_custody(home, registry, backend, monkeypatch, park):
    from tools.process_registry_awaiting import pending_events, release
    from tui_gateway import notification_park
    _, event = _finished_event(registry, exit_code=6)
    backend.sessions["sid"] = _session(home)
    monkeypatch.setattr(notification_park, "_parked", [])
    if park == "registry":
        registry.park_unowned_event(event)
        claimed = registry.take_parked_events(lambda evt: True)
    else:
        assert notification_park.park(event)
        claimed = notification_park.claim(lambda evt: True)
    try:
        assert claimed == [event]
        assert event in pending_events(registry.completion_queue)
        server._shutdown_sessions()
        marker = read_turn_marker(home, "session-key")
        assert marker is not None
        assert [(j["session_id"], j["exit_code"]) for j in marker["killed_jobs"]] == [("proc_done", 6)]
    finally:
        release(claimed)
        if park == "notification":
            notification_park.unhold(claimed)
    assert not any(e is event for e in pending_events(registry.completion_queue))


def test_registry_park_claim_rechecks_presence(registry):
    from tools.process_registry_awaiting import release
    event = {"session_id": "proc_claim", "owner_task_id": "owner"}
    registry.park_unowned_event(event)
    inner = []

    def owns(evt):
        inner.extend(registry.take_parked_events(lambda e: True))
        return True

    try:
        assert registry.take_parked_events(owns) == []
        assert inner == [event]
    finally:
        release(inner)


def test_registry_park_routing_releases_custody_on_error(home, registry, backend, monkeypatch):
    from tools.process_registry_awaiting import pending_events
    _, event = _finished_event(registry)
    session = backend.sessions["sid"] = _session(home)
    registry.park_unowned_event(event)

    def handle(events, deferred):
        assert any(e is event for e in pending_events(registry.completion_queue))
        raise RuntimeError("routing refused")

    assert server._notif_take_parked("sid", session, registry, handle) == 0
    assert not any(e is event for e in pending_events(registry.completion_queue))


def test_natural_exit_between_running_snapshot_and_kill_reports_real_result(home, registry, backend, monkeypatch):
    job = _job("proc_race", owner="turn-1", command="gh run watch 9", notify=True)
    registry._running[job.id] = job
    backend.sessions["sid"] = _session(home)
    real_kill = registry.kill_process

    def exits_first(sid, **kw):
        job.append_output("CI passed\n")
        registry._finish_exited(job, 0)  # finished on its own just before the shutdown signal
        return real_kill(sid, **kw)

    monkeypatch.setattr(registry, "kill_process", exits_first)
    server._shutdown_sessions()
    receipts = read_turn_marker(home, "session-key")["killed_jobs"]
    assert [(j["session_id"], j["kill_state"], j["exit_code"]) for j in receipts] == [("proc_race", "completed", 0)]
    registry.completion_queue.queue.clear()
    monkeypatch.setattr("tools.process_registry.process_registry", ProcessRegistry())
    text = _resume_text(monkeypatch, home)
    assert text.count("CI passed") == 1 and "restart it" not in text and "interruption outcome" not in text


@pytest.mark.parametrize("shape", ["rebuilt_agent", "ownerless_legacy"])
def test_pending_ownership_follows_live_routing(home, registry, backend, shape):
    """Addressed events follow session-key routing, not the agent's current owner-id set (a rebuilt agent
    loses older ids); ownerless legacy events go to the session whose agent spawned them."""
    _, event = _finished_event(registry)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    if shape == "rebuilt_agent":
        session["agent"]._process_owner_task_ids = ("turn-after-rebuild",)
    else:
        event.pop("session_key")
    registry.completion_queue.put(event)
    server._shutdown_sessions()
    marker = read_turn_marker(home, "session-key")
    assert marker is not None and [j["session_id"] for j in marker["killed_jobs"]] == ["proc_done"]


def test_pending_completion_of_another_profiles_chat_stays_with_that_chat(home, registry, backend):
    other_home = home / "other-profile"
    other_home.mkdir()
    _, event = _finished_event(registry, session_key="other-key")  # same raw owner id, other chat
    registry.completion_queue.put(event)
    backend.sessions["sid"] = _session(home, _notif_held=[])
    other = backend.sessions["sid-other"] = _session(other_home, _notif_held=[])
    other["session_key"] = "other-key"
    server._shutdown_sessions()
    assert read_turn_marker(home, "session-key") is None
    receipts = read_turn_marker(other_home, "other-key")["killed_jobs"]
    assert [j["session_id"] for j in receipts] == ["proc_done"]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_startup_sweep_resumes_a_job_receipt_older_than_the_turn_freshness_window(home, registry, backend):
    """A Desktop closed for an hour still gets the report: the receipt is not a stale turn."""
    from tui_gateway import startup_resume

    notify = _job("proc_notify", owner="turn-1", command="sleep 600", notify=True)
    registry._running[notify.id] = notify
    backend.sessions["sid"] = _session(home)
    server._shutdown_sessions()

    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["session-key"]["started_at"] = time.time() - 3600
    entries["session-key"]["writer_pid"] = _dead_pid()
    entries["session-key"].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))

    rows = startup_resume.pending_resumes(home, CFG)
    assert [r["session_key"] for r in rows] == ["session-key"]
    calls = []
    startup_resume.resume_interrupted_sessions(lambda rid, params: calls.append(params) or {}, home, CFG,
                                               paused_only=True)
    assert [c["session_id"] for c in calls] == ["session-key"]



def _rcpt(sid, state="completed", notify=True):
    return {"session_id": sid, "command": f"cmd {sid}", "started_at": 1.0, "notify_on_complete": notify,
            "kill_state": state}


def _late_marker_session(home):
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    session = _session(home)
    session["history_lock"] = threading.Lock()
    session[COMPLETION_UNSECURED] = {"P1": {"session_id": "P1"}}
    return session


@pytest.mark.parametrize("order", ["journal_then_marker", "marker_then_journal"])
def test_late_notice_marker_and_exit_journal_keep_every_receipt(home, order):
    """A notice turn (carrying P1) whose marker lands after or before the exit journal wrote P1, P2 and a
    kill receipt: the final marker carries the turn's prompt, P2 and the kill receipt, and P1 exactly once."""
    from tui_gateway.exit_job_receipts import _save_receipts
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    session = _late_marker_session(home)
    journal = [_rcpt("P1"), _rcpt("P2"), _rcpt("K1", state="killed")]
    if order == "journal_then_marker":
        assert _save_receipts(home, "session-key", journal)
        assert server._record_turn_marker(session, "notice P1 finished") == "session-key"
    else:
        assert server._record_turn_marker(session, "notice P1 finished") == "session-key"
        assert _save_receipts(home, "session-key", [_rcpt("P2"), _rcpt("K1", state="killed")])
    marker = read_turn_marker(home, "session-key")
    assert marker["prompt"] == "notice P1 finished"
    assert sorted(j["session_id"] for j in marker["killed_jobs"]) == ["K1", "P2"]
    assert not session.get(COMPLETION_UNSECURED)


def test_failed_marker_write_keeps_notice_custody(home, monkeypatch):
    from tui_gateway import turn_marker
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    session = _late_marker_session(home)
    monkeypatch.setattr(turn_marker, "_store", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    server._record_turn_marker(session, "notice P1 finished")
    assert set(session[COMPLETION_UNSECURED]) == {"P1"}


def test_user_stop_before_late_marker_keeps_journaled_receipts(home):
    from tui_gateway.exit_job_receipts import JOBS_REASON, _save_receipts
    session = _late_marker_session(home)
    assert _save_receipts(home, "session-key", [_rcpt("P1"), _rcpt("P2"), _rcpt("K1", state="killed")])
    session["_turn_cancel_requested"] = True
    server._record_turn_marker(session, "notice P1 finished")
    marker = read_turn_marker(home, "session-key")
    assert marker["reason"] == JOBS_REASON
    assert sorted(j["session_id"] for j in marker["killed_jobs"]) == ["K1", "P2"]


def test_marker_then_exit_journal_then_cancelled_retire_keeps_journaled_receipt(home):
    """Retiring the cancelled notice turn must preserve a later journaled completion."""
    from tui_gateway.exit_job_receipts import JOBS_REASON, _save_receipts
    from tui_gateway.turn_marker import retire_cancelled_turn_marker
    assert record_turn_start(home, "session-key", "notice P1 finished", carried_job_ids=["P1"])
    assert _save_receipts(home, "session-key", [_rcpt("P2")])
    retire_cancelled_turn_marker(home, "session-key")
    marker = read_turn_marker(home, "session-key")
    assert marker is not None and marker["reason"] == JOBS_REASON
    assert [j["session_id"] for j in marker["killed_jobs"]] == ["P2"]
