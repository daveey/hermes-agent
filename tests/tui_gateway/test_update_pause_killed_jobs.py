"""A core-update pause must not silently drop a resuming chat's background jobs.

At a successful pause boundary, owned non-persist jobs are stopped with source
``core_update_pause`` and ``consume_output=False``. The jobs actually killed are
written into the durable interrupted-turn marker before the pause is released, and
the resume note lists each one (id, command head, start time, restart advice). A
``notify=true`` job also carries the normal completion text with reason
"killed by the Hermes update". That note is read back from the marker file, not
from the in-memory completion queue. Session-end close and ``persist_on_release``
stay as they are; a foreign owner's job is not stopped.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_notifications import format_process_notification

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True,
                                     "freshness_minutes": 240, "max_attempts": 2}}}
STARTED = 1_700_000_000.0
KILL_REASON = "killed by the Hermes update"


class _LiveProc:
    """Stays running until the kill path signals it, so list/reconcile cannot steal the exit."""

    def __init__(self):
        self.pid = 4242
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


def _session(agent=None, **extra):
    return {"agent": agent if agent is not None else types.SimpleNamespace(),
            "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
            "history_version": 0, "running": False, "attached_images": [], "image_counter": 0,
            "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
            "inflight_turn": None, **extra}


def _job(sid, *, owner, command, notify=False, persist=False, session_key="session-key"):
    session = ProcessSession(id=sid, command=command, task_id=owner, owner_task_id=owner,
                             session_key=session_key, started_at=STARTED)
    session.notify_on_complete = notify
    session.persist_on_release = persist
    session.process = _LiveProc()
    return session


def _start(epoch=STARTED) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    return tmp_path


@pytest.fixture()
def registry(monkeypatch):
    reg = ProcessRegistry()

    def _stop(pid, expected_start=None):
        for session in (*reg._running.values(), *reg._finished.values()):
            proc = session.process
            if proc is not None and getattr(proc, "pid", None) == pid:
                proc._alive = False

    monkeypatch.setattr(reg, "_terminate_host_pid", _stop)
    monkeypatch.setattr(reg, "_write_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr("tools.process_registry.save_completed_result", lambda *a, **k: None)
    monkeypatch.setattr("tools.process_registry.process_registry", reg)
    return reg


def _request_pause(root):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120)


def _install(registry, *sessions):
    for session in sessions:
        registry._running[session.id] = session


def test_update_pause_records_killed_jobs_and_resume_note_lists_them(home, registry, monkeypatch):
    """Pause → durable marker → resumed note, including a notify completion that outlives the queue."""
    notify = _job("proc_notify", owner="turn-1", command="sleep 600", notify=True)
    quiet = _job("proc_quiet", owner="turn-1", command="echo quiet-job")
    persisted = _job("proc_persist", owner="turn-1", command="sleep 9999", notify=True, persist=True)
    foreign = _job("proc_foreign", owner="other-turn", command="sleep 42", notify=True,
                   session_key="other-session")
    _install(registry, notify, quiet, persisted, foreign)
    record_turn_start(home, "session-key", "fix the flaky test")
    _request_pause(home)
    agent = types.SimpleNamespace(_process_owner_task_ids=("turn-1",))
    session = _session(agent=agent, profile_home=str(home))

    assert up.boundary_pause_hook(session, "session-key")() == up.PAUSE_REASON

    assert notify.exited is True and notify.termination_source == up.PAUSE_REASON
    assert quiet.exited is True and quiet.termination_source == up.PAUSE_REASON
    assert notify.id not in registry._completion_consumed
    assert quiet.id not in registry._completion_consumed
    assert persisted.exited is False and foreign.exited is False

    on_disk = json.loads((home / "desktop" / "interrupted_turns.json").read_text())
    stored = on_disk["session-key"]["killed_jobs"]
    assert [job["session_id"] for job in stored] == ["proc_notify", "proc_quiet"]
    by_id = {job["session_id"]: job for job in stored}
    assert by_id["proc_notify"]["command"] == "sleep 600"
    assert by_id["proc_notify"]["started_at"] == STARTED
    assert by_id["proc_notify"]["notify_on_complete"] is True
    assert by_id["proc_quiet"]["notify_on_complete"] is False
    assert "proc_persist" not in by_id and "proc_foreign" not in by_id

    # The resume path reads the marker file. Drop the in-memory queue first so a
    # passing note cannot be explained by a completion that dies with the process.
    registry.completion_queue.queue.clear()
    marker = read_turn_marker(home, "session-key")
    assert [job["session_id"] for job in marker["killed_jobs"]] == ["proc_notify", "proc_quiet"]

    # The next backend resumes after the update window releases the hold. Leaving it
    # set makes admission refuse the continuation, which is not the resume path.
    af.clear_pause_request(home)
    af.clear_hold(home)
    submitted = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_run_prompt_submit",
                        lambda rid, sid, session, text, **kw: submitted.append(text))
    fresh = _session(profile_home=str(home))
    assert server._maybe_schedule_auto_continue("sid", fresh, "session-key") is not None
    (text,) = submitted
    assert text.startswith(up.pause_note("fix the flaky test"))
    started = _start()
    for sid, command in (("proc_notify", "sleep 600"), ("proc_quiet", "echo quiet-job")):
        assert sid in text and command in text and started in text
        assert "restart it if you still need it" in text
    notice = format_process_notification({
        "type": "completion", "session_id": "proc_notify", "command": "sleep 600",
        "exit_code": -15, "completion_reason": "killed", "termination_source": up.PAUSE_REASON,
        "kill_reason": KILL_REASON, "output": "",
    })
    assert notice in text
    assert KILL_REASON in notice
    assert "Background process proc_quiet" not in text
    assert "proc_persist" not in text and "proc_foreign" not in text
    assert registry.completion_queue.empty()


def test_agent_close_still_kills_owned_jobs_and_skips_persist(registry, monkeypatch):
    """A real session end still kills with agent_close and consume_output=True; persist is unchanged."""
    from agent.client_lifecycle import ClientLifecycleMixin
    import run_agent as ra
    import tools.computer_use.tool as cu

    owned = _job("proc_owned", owner="turn-1", command="sleep 600", notify=True)
    persisted = _job("proc_persist", owner="turn-1", command="sleep 9999", persist=True)
    _install(registry, owned, persisted)
    monkeypatch.setattr(ra, "cleanup_vm", lambda *a, **k: None)
    monkeypatch.setattr(ra, "cleanup_browser", lambda *a, **k: None)
    monkeypatch.setattr(cu, "release_computer_use_session", lambda *a, **k: None)
    agent = types.SimpleNamespace(_process_owner_task_ids=("turn-1",))

    ClientLifecycleMixin._close_task_resources(agent, "turn-1")

    assert owned.exited is True and owned.termination_source == "agent_close"
    assert owned.id in registry._completion_consumed
    assert persisted.exited is False


def test_lifecycle_kill_is_logged_with_session_process_and_source(registry, caplog):
    """Lifecycle kills are logged once, centrally, with the chat session, process id and source."""
    owned = _job("proc_owned", owner="turn-1", command="sleep 600", notify=True)
    _install(registry, owned)

    with caplog.at_level(logging.INFO, logger="tools.process_registry"):
        result = registry.kill_process(owned.id, source="agent_close", consume_output=True)

    assert result["status"] == "killed"
    matches = [r for r in caplog.records if r.levelno == logging.INFO and "lifecycle kill" in r.getMessage()]
    assert len(matches) == 1
    message = matches[0].getMessage()
    assert "session=session-key" in message
    assert "process=proc_owned" in message
    assert "source=agent_close" in message


def test_operator_kill_is_not_a_lifecycle_log(registry, caplog):
    """An explicit stop is not a lifecycle kill and does not use that log line."""
    owned = _job("proc_owned", owner="turn-1", command="sleep 600")
    _install(registry, owned)

    with caplog.at_level(logging.INFO, logger="tools.process_registry"):
        registry.kill_process(owned.id, source="process.kill", consume_output=True)

    assert not any("lifecycle kill" in r.getMessage() for r in caplog.records)
