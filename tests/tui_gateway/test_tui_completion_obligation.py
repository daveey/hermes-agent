"""A Desktop chat's finished notify job survives an unclean death of the Desktop backend that held its notice.

``_move_to_finished`` records the job's receipt and drops its checkpoint entry before the chat's poller turns
the completion into a notice turn. A SIGKILL/OOM of the backend in that gap (no orderly exit journal) used to
lose the notice: nothing was left running to re-adopt, and recovery reads a receipt as "observed".

The receipt now carries the owed delivery for a Desktop-owned (``owner: tui``) notify job too
(``tools/process_registry_obligations.py``). It is settled when the notice turn starts, on an intended
suppression (wait/log/kill, notifications off) or when the orderly-exit journal takes custody
(``tui_gateway/completion_obligation.py``). The next Desktop backend re-arms an owed receipt whose writer is
gone, once, through the re-adopt sink, which parks it for the chat and wakes the chat when it is not loaded.

These tests use the real registry, real receipts and checkpoint files in the test home, the real re-adopt
sink, the real poller dispatch and the real start commit. Only the worker start is stubbed (no model runs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import pytest

from hermes_cli import process_identity
from tests.tui_gateway import test_shutdown_job_receipts as sjr
from tools import process_registry as pr_mod
from tools import process_registry_obligations as obligations
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_obligations import OBLIGATION_KEY
from tools.process_registry_readopt import recover_tui_orphaned_processes
from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

STARTED = 1_794_505_311.25
_REAL_IS_DESKTOP_OWNED = process_identity.is_desktop_owned_backend
OUTPUT = "build ok\nCI passed: release verified (RESULT_proc_desk0001)\n"
_LiveProc, _resume_text, _session = sjr._LiveProc, sjr._resume_text, sjr._session
_slow_notice_turn = sjr._slow_notice_turn


@pytest.fixture()
def home(monkeypatch, tmp_path):
    """One home for the turn markers and the process receipts/checkpoint (as in a Desktop backend)."""
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: sjr.CFG)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 15 * 60.0, 2), raising=False)
    monkeypatch.setattr("hermes_cli.update_lock.update_in_progress", lambda *a, **k: False)
    monkeypatch.setattr(sjr.up, "_detect_skew", lambda: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def backend(monkeypatch, registry):
    """The exit path with the real agent-close kill (``test_shutdown_job_receipts.backend``)."""
    from agent.client_lifecycle import ClientLifecycleMixin
    import run_agent as ra
    import tools.computer_use.tool as cu

    monkeypatch.setattr(ra, "cleanup_vm", lambda *a, **k: None)
    monkeypatch.setattr(ra, "cleanup_browser", lambda *a, **k: None)
    monkeypatch.setattr(cu, "release_computer_use_session", lambda *a, **k: None)
    sessions: dict = {}

    def close(sid, *, end_reason="tui_close", predicate=None):
        session = sessions.pop(sid, None)
        for owner in (session["agent"]._process_owner_task_ids if session else ()):
            ClientLifecycleMixin._close_task_resources(session["agent"], owner)
        return session is not None

    monkeypatch.setattr(server, "_sessions", sessions)
    monkeypatch.setattr(server, "_close_session_by_id", close)
    for name in ("_flush_sessions_before_exit", "_release_gateway_wake_owner", "_stop_turns_before_exit"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    return types.SimpleNamespace(sessions=sessions)


@pytest.fixture()
def registry(monkeypatch, home):
    """Backend A: a Desktop-owned backend whose receipts and checkpoint live in the test home."""
    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: True)
    reg = ProcessRegistry()

    def _stop(pid, expected_start=None):
        for job in (*reg._running.values(), *reg._finished.values()):
            if job.process is not None and getattr(job.process, "pid", None) == pid:
                job.process._alive = False

    monkeypatch.setattr(reg, "_terminate_host_pid", _stop)
    monkeypatch.setattr(pr_mod, "process_registry", reg)
    return reg


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _as_dead_writer(monkeypatch):
    """The receipt's writer stamp names a backend that is gone (the test itself stands in for both backends)."""
    dead = _dead_pid()
    monkeypatch.setattr(obligations, "_writer", lambda: {"writer_pid": dead, "writer_start_time": None})


def _job(reg, sid="proc_desk0001", *, notify=True, key="session-key", platform="", interval=0):
    job = ProcessSession(id=sid, command="gh run watch 77 && ./deploy.sh", task_id="turn-1", owner_task_id="turn-1",
                         session_key=key, started_at=STARTED, notify_on_complete=notify,
                         watcher_platform=platform, watcher_chat_id="1" if platform else "",
                         watcher_interval=interval)
    job.process = _LiveProc(4242)
    with reg._lock:
        reg._running[sid] = job
    job.append_output(OUTPUT)
    return job


def _finish(reg, job, exit_code=3):
    assert reg._finish_exited(job, exit_code) is True


def _receipt(home, sid="proc_desk0001") -> dict:
    return json.loads((home / "logs" / "process-results" / f"{sid}.json").read_text())


def _next_backend(monkeypatch, owner="tui"):
    """A fresh backend process: new registry, the Desktop re-adopt sink wired, startup recovery as it runs."""
    reg = ProcessRegistry()
    monkeypatch.setattr(pr_mod, "process_registry", reg)
    monkeypatch.setattr(reg, "start_detached_sweep", lambda *a, **k: False)
    wakes, threads = [], []
    monkeypatch.setattr(server, "_wake_chat_for_readopted_exit", wakes.append)
    reg.on_unknown_exit = lambda evt: threads.append(server._readopted_exit_deliver(evt, registry=reg))
    if owner == "tui":
        armed = recover_tui_orphaned_processes(reg)
    else:
        armed = reg.recover_from_checkpoint(owner=owner)
    for thread in threads:
        if thread is not None:
            thread.join(5)
    return reg, armed, wakes


def _open_chat_and_poll(reg, session):
    """The chat's poller: take the parked events it owns and route them (status row, notice turn)."""
    from tools.process_registry_notifications import format_process_notification
    emitted: set = set()
    return server._notif_take_parked("sid", session, reg, lambda events, deferred: server._notif_handle_ready(
        "sid", session, events, emitted, reg, format_process_notification, deferred))


def test_unclean_backend_death_after_settle_delivers_the_notice_once(home, registry, backend, monkeypatch):
    _as_dead_writer(monkeypatch)
    job = _job(registry)
    _finish(registry, job, 3)
    receipt = _receipt(home)
    assert receipt["exit_code"] == 3
    assert receipt.get(OBLIGATION_KEY, {}).get("owner") == "tui", "the owed notice must ride the receipt write"
    assert "proc_desk0001" not in (home / "processes.json").read_text(), "the checkpoint entry is gone"

    # SIGKILL: no journal, no poller, the in-memory completion is gone with the process.
    reg, armed, wakes = _next_backend(monkeypatch)
    assert armed == 1, "the next Desktop backend lost custody of the owed completion"
    assert wakes == ["session-key"], "an unloaded chat is woken for its owed notice"
    assert reg.has_parked_events()

    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    turn = _slow_notice_turn(monkeypatch, block_in=None)
    assert _open_chat_and_poll(reg, session) == 1
    assert turn.outcomes == [True] and len(turn.starts) == 1
    (text,) = turn.texts
    assert text.count("CI passed: release verified (RESULT_proc_desk0001)") == 1
    assert "proc_desk0001" in text and "3" in text
    assert OBLIGATION_KEY not in _receipt(home), "the started notice turn settles the obligation"

    # Acknowledged: later Desktop starts never offer it again.
    for _ in range(2):
        reg, armed, wakes = _next_backend(monkeypatch)
        assert (armed, wakes, reg.has_parked_events()) == (0, [], False)
    assert _open_chat_and_poll(reg, session) == 0 and len(turn.starts) == 1


def test_owed_notice_parks_while_the_writer_backend_lives(home, registry, monkeypatch):
    """Blue/green: the old backend still runs (it holds the completion in memory), so the new one leaves it."""
    job = _job(registry)
    _finish(registry, job, 0)
    assert _receipt(home)[OBLIGATION_KEY]["writer_pid"] == os.getpid()
    reg, armed, wakes = _next_backend(monkeypatch)
    assert (armed, wakes, reg.has_parked_events()) == (0, [], False)
    assert OBLIGATION_KEY in _receipt(home)


def _other_profile(registry, home, monkeypatch):
    receipt_home = home / "profiles" / "other"
    receipt_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(receipt_home))
    _finish(registry, _job(registry))
    monkeypatch.setenv("HERMES_HOME", str(home))
    return receipt_home


def _cli_process(registry, home, monkeypatch):
    """A CLI/worker process delivers in-process: it never leaves a Desktop obligation."""
    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: False)
    _finish(registry, _job(registry))
    return home


# case -> (backend A's job, owed owner on its receipt, recovering owner)
_FOREIGN_CASES = {
    "not_notify": (lambda reg, home, mp: _finish(reg, _job(reg, notify=False)) or home, None, "tui"),
    "cli_process": (_cli_process, None, "tui"),
    "other_profile": (_other_profile, "tui", "tui"),
    "gateway_owned": (lambda reg, home, mp: _finish(reg, _job(
        reg, key="agent:main:telegram:dm:1", platform="telegram", interval=5)) or home, "gateway", "tui"),
    # A Desktop-owned obligation is never adopted by a messaging gateway start.
    "gateway_start": (lambda reg, home, mp: _finish(reg, _job(reg)) or home, "tui", "gateway"),
}


@pytest.mark.parametrize("case", sorted(_FOREIGN_CASES))
def test_only_this_profiles_desktop_owed_notices_wake(home, registry, monkeypatch, case):
    _as_dead_writer(monkeypatch)
    finish, owner, recovering = _FOREIGN_CASES[case]
    receipt_home = finish(registry, home, monkeypatch)
    owed = _receipt(receipt_home).get(OBLIGATION_KEY)
    assert (owed or {}).get("owner") == owner
    reg, armed, wakes = _next_backend(monkeypatch, owner=recovering)
    assert (armed, wakes, reg.has_parked_events(), reg.pending_watchers) == (0, [], False, [])
    assert _receipt(receipt_home).get(OBLIGATION_KEY) == owed, "another owner's obligation was touched"


@pytest.mark.parametrize("consume", ["wait", "log", "kill", "quiet"])
def test_consumed_or_quiet_completion_is_not_redelivered(home, registry, backend, monkeypatch, consume):
    _as_dead_writer(monkeypatch)
    job = _job(registry)
    _finish(registry, job, 0)
    assert OBLIGATION_KEY in _receipt(home)
    if consume == "kill":  # the explicit tool kill of a just-exited job hands the output to its caller
        registry.kill_process(job.id)
    elif consume == "wait":
        registry.wait(job.id, timeout=1)
    elif consume == "log":
        registry.read_log(job.id)
    else:  # the chat's profile has background notifications off: the status row is the whole delivery
        monkeypatch.setattr(server, "_load_cfg", lambda: {"display": {"background_process_notifications": "off"}})
        session = backend.sessions["sid"] = _session(home, _notif_held=[])
        turn = _slow_notice_turn(monkeypatch, block_in=None)
        registry.park_unowned_event(registry.completion_queue.get_nowait())
        assert _open_chat_and_poll(registry, session) == 1 and turn.starts == []
    assert OBLIGATION_KEY not in _receipt(home), f"{consume} must settle the owed notice"
    reg, armed, wakes = _next_backend(monkeypatch)
    assert (armed, wakes, reg.has_parked_events()) == (0, [], False)


def test_notice_turn_start_reads_only_receipts_this_backend_owes(home, registry, monkeypatch):
    """``settle_started`` runs on every notice-turn start: a job that never carried an obligation costs no receipt
    read; an owed one (written here, or re-armed by the next backend) is still settled."""
    from tui_gateway.completion_obligation import settle_started
    _as_dead_writer(monkeypatch)
    _finish(registry, _job(registry, "proc_quiet0001", notify=False))
    _finish(registry, _job(registry, "proc_desk0001"))
    assert OBLIGATION_KEY not in _receipt(home, "proc_quiet0001") and OBLIGATION_KEY in _receipt(home)
    reads: list = []
    real_path = obligations.receipt_path
    monkeypatch.setattr(obligations, "receipt_path", lambda sid: reads.append(sid) or real_path(sid))

    settle_started(["proc_quiet0001", "proc_never0001"])
    assert reads == [], "a pid with no owed tui obligation must not touch its receipt"

    _reg, armed, _ = _next_backend(monkeypatch)  # the owed one, re-armed by a fresh backend
    assert armed == 1
    reads.clear()
    settle_started(["proc_desk0001", "proc_quiet0001"])
    assert set(reads) == {"proc_desk0001"} and OBLIGATION_KEY not in _receipt(home)
    reads.clear()
    settle_started(["proc_desk0001"])  # settled: dropped from memory, no further reads
    assert reads == []


@pytest.mark.parametrize("when", ["finished", "running"])
def test_orderly_exit_journal_takes_custody_and_delivers_once(home, registry, backend, monkeypatch, when):
    """A finished notice the chat had not run yet, or a running job the exit stops: the turn marker reports it."""
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit
    _as_dead_writer(monkeypatch)
    job = _job(registry)
    backend.sessions["sid"] = _session(home, _notif_held=[])
    if when == "finished":
        _finish(registry, job, 0)
        assert _receipt(home)[OBLIGATION_KEY]["owner"] == "tui"
    journal_background_jobs_before_exit()
    server._shutdown_sessions()
    marker = read_turn_marker(home, "session-key")
    assert [(j["session_id"], j["kill_state"]) for j in marker["killed_jobs"]] == [
        ("proc_desk0001", "completed" if when == "finished" else "killed")]
    assert OBLIGATION_KEY not in _receipt(home), "the journal took custody: the receipt must not deliver it again"

    registry.completion_queue.queue.clear()  # the next backend has no in-memory queue
    reg, armed, wakes = _next_backend(monkeypatch)
    assert (armed, wakes, reg.has_parked_events()) == (0, [], False)
    text = _resume_text(monkeypatch, home)
    assert text.count("proc_desk0001") >= 1
    if when == "finished":
        assert text.count("CI passed: release verified") == 1


def test_managed_worker_of_a_desktop_chat_owes_its_finished_notice_to_the_desktop_keeper(home, registry, backend,
                                                                                          monkeypatch):
    """A managed worker's Desktop completion queue dies with its turn; the Desktop keeper
    retains and re-arms its notice obligation. Exec no longer inherits the Desktop token;
    the explicit identity fixture here exercises keeper recovery, not env propagation."""
    from agent import runtime_session_store
    from gateway.session_managed_worker import _worker_env
    from tools.process_registry_readopt import _checkpoint_keeper_tick

    # The worker's real identity check, not the fixture's stub: inherited Desktop flag + per-spawn token.
    monkeypatch.setattr(process_identity, "is_desktop_owned_backend", _REAL_IS_DESKTOP_OWNED)
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-spawn-token")
    monkeypatch.setattr(runtime_session_store, "_worker_process", True)
    child_env = _worker_env(types.SimpleNamespace(profile_id=str(home)))
    assert 'HERMES_DASHBOARD_SESSION_TOKEN' not in child_env
    assert process_identity.is_desktop_owned_backend() is True

    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stamp = {"writer_pid": worker.pid, "writer_start_time": ProcessRegistry._safe_host_start_time(worker.pid)}
        monkeypatch.setattr(obligations, "_writer", lambda: dict(stamp))
        _finish(registry, _job(registry), 3)  # a bare (Desktop) session key: owner tui
        owed = _receipt(home)[OBLIGATION_KEY]
        assert (owed["owner"], owed["writer_pid"]) == ("tui", worker.pid)

        # The Desktop backend's keeper ticks while the worker still runs: the worker holds it, nothing re-arms.
        monkeypatch.setattr(runtime_session_store, "_worker_process", False)
        desktop, armed, wakes = _next_backend(monkeypatch)
        _checkpoint_keeper_tick(desktop)
        assert (armed, wakes, desktop.has_parked_events()) == (0, [], False)
        assert _receipt(home)[OBLIGATION_KEY]["writer_pid"] == worker.pid
    finally:
        worker.kill()
        worker.wait()

    # The worker exited (its in-memory completion went with it): the keeper's next tick re-arms it, once.
    threads, wakes = [], []
    desktop.on_unknown_exit = lambda evt: threads.append(server._readopted_exit_deliver(evt, registry=desktop))
    monkeypatch.setattr(server, "_wake_chat_for_readopted_exit", wakes.append)
    _checkpoint_keeper_tick(desktop)
    _checkpoint_keeper_tick(desktop)  # this backend now holds it: a second tick does not re-arm it again
    for thread in threads:
        if thread is not None:
            thread.join(5)
    assert wakes == ["session-key"] and desktop.has_parked_events()

    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    turn = _slow_notice_turn(monkeypatch, block_in=None)
    assert _open_chat_and_poll(desktop, session) == 1
    (text,) = turn.texts
    assert text.count("CI passed: release verified (RESULT_proc_desk0001)") == 1
    assert OBLIGATION_KEY not in _receipt(home)


@pytest.mark.parametrize("exit_kind", ["orderly", "unclean"])
def test_update_pause_kill_is_reported_once_from_the_marker(home, registry, backend, monkeypatch, exit_kind):
    """A core-update pause kills the chat's notify job with ``consume_output=False`` and journals the kill into
    the marker's ``killed_jobs``. Whether the backend then exits in order (exit journal) or dies (no journal),
    the next backend reports the kill once, from the marker, and never re-arms the receipt's obligation as a
    second notice beside it."""
    from hermes_cli import admission_fence as af
    from tui_gateway import update_pause as up
    from tui_gateway.exit_job_receipts import journal_background_jobs_before_exit

    _as_dead_writer(monkeypatch)
    job = _job(registry)
    session = _pause(home, backend)
    assert job.exited and job.termination_source == up.PAUSE_REASON
    assert [j["session_id"] for j in read_turn_marker(home, "session-key")["killed_jobs"]] == ["proc_desk0001"]

    if exit_kind == "orderly":
        journal_background_jobs_before_exit()
        server._shutdown_sessions()
    registry.completion_queue.queue.clear()  # the next backend has no in-memory queue
    af.clear_pause_request(home)
    af.clear_hold(home)
    assert session.get("_update_pause_marker_key") == "session-key"

    reg, armed, wakes = _next_backend(monkeypatch)
    assert (armed, wakes, reg.has_parked_events()) == (0, [], False), "the killed job's notice was re-armed"
    assert OBLIGATION_KEY not in _receipt(home), "the marker carries the notice: the receipt must not owe it too"
    text = _paused_resume_text(monkeypatch, home)
    assert text.count("Background process proc_desk0001") == 1 and up.UPDATE_KILL_REASON in text
    reg, armed, wakes = _next_backend(monkeypatch)
    assert (armed, wakes, reg.has_parked_events()) == (0, [], False)


def _pause(home, backend, stands=True, *, end_turn=True):
    """A chat's turn reaches a step boundary while a core update asks for the pause, then (``end_turn``) ends
    paused the way ``prompt_turn`` ends it (``pause_stands``)."""
    import time

    from hermes_cli import admission_fence as af
    from tui_gateway import update_pause as up
    record_turn_start(home, "session-key", "ship the release")
    af.set_hold(home)
    af.request_pause(home, expires_at=time.time() + 120)
    session = backend.sessions["sid"] = _session(home, _notif_held=[])
    assert up.boundary_pause_hook(session, "session-key")() == (up.PAUSE_REASON if stands else None)
    if stands and end_turn:
        assert up.pause_stands(session, "session-key", {"paused_for_update": True}) is True
    return session


def test_update_pause_a_later_stop_beats_keeps_the_obligation(home, registry, backend, monkeypatch):
    """The hook paid the pause (jobs killed, marker stamped), but the user's Stop lands before the turn ends
    (``user_stop_wins``): the pause never stands and the stopped turn's marker retires with its killed_jobs. The
    receipt's obligation is then the notice's only durable custody, so it must survive until the turn's end and
    an unclean death after it re-arms the notice once, never loses it."""
    from tui_gateway import update_pause as up
    _as_dead_writer(monkeypatch)
    job = _job(registry)
    session = _pause(home, backend, end_turn=False)
    assert job.exited and job.termination_source == up.PAUSE_REASON
    assert _receipt(home)[OBLIGATION_KEY]["owner"] == "tui", "settled before the pause stood"
    session.update(running=True, _active_turn_marker_key="session-key")
    assert up.user_stop_wins(session) is True
    assert up.pause_stands(session, "session-key", {"interrupted": True}) is False
    assert _receipt(home)[OBLIGATION_KEY]["owner"] == "tui", "a pause a later Stop beat settled the notice"

    registry.completion_queue.queue.clear()  # unclean death: the in-memory completion goes with the backend
    reg, armed, _wakes = _next_backend(monkeypatch)
    assert armed == 1 and reg.has_parked_events(), "the killed job's notice was lost"


@pytest.mark.parametrize("race", ["exited_before_kill", "user_stop_won"])
def test_update_pause_keeps_the_obligation_the_marker_does_not_carry(home, registry, backend, monkeypatch, race):
    """Controls. A job that finished on its own just before the pause's kill (``already_exited``) is in the marker
    only as an uncertain outcome, without its real output. A pause a user Stop beat after the kills does not
    stand: the turn goes on and its queued completion is delivered the normal way. Both keep the owed notice."""
    from tui_gateway import update_pause as up
    _as_dead_writer(monkeypatch)
    job = _job(registry)
    if race == "exited_before_kill":
        real_kill = registry.kill_process

        def exits_first(sid, **kw):
            _finish(registry, job, 0)
            return real_kill(sid, **kw)

        monkeypatch.setattr(registry, "kill_process", exits_first)
        _pause(home, backend)
        (receipt,) = read_turn_marker(home, "session-key")["killed_jobs"]
        assert receipt["kill_state"] == "already_exited"
    else:
        checks = iter([False, True])  # no Stop before the kills; the Stop is seen under the lock after them
        monkeypatch.setattr(up, "user_stop_pending", lambda session: next(checks))
        _pause(home, backend, stands=False)
        assert job.exited and job.termination_source == up.PAUSE_REASON
    assert _receipt(home)[OBLIGATION_KEY]["owner"] == "tui", "the marker does not carry this notice"


def _paused_resume_text(monkeypatch, home) -> str:
    """The next backend's auto-continue of the paused turn (``_resume_text`` with the paused-turn slot call)."""
    submitted = []
    monkeypatch.setattr(server.threading, "Thread", sjr._InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session, **kw: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: submitted.append(text))
    assert server._maybe_schedule_auto_continue("sid", _session(home), "session-key") is not None
    (text,) = submitted
    return text
