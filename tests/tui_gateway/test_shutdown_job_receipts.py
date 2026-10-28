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
    for name in ("_flush_sessions_before_exit", "_release_gateway_wake_owner", "_stop_turns_before_exit"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    return types.SimpleNamespace(sessions=sessions, closed=closed, close=close)


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
