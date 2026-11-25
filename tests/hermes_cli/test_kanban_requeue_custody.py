"""A core-update requeue keeps the retry off other hosts while the run's processes live (t_01a1dae3ec3f5b57).

Incident t_01a1d85b7b4a9e6d (2026-11-14): hermes-sync's restart-safe requeue reclaimed run 48261 on worker-2,
and worker-8 claimed the retry 2 s later while worker-2's ship of PR #2946, started by that run, kept pushing.
Real child processes stand in for the run's jobs; the run's systemd scope is the one injected seam.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_requeue_custody as custody
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.waiting import wait_until

SESSION = "local-run-48261-session"
HOST, OTHER = "worker-2", "worker-8"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(custody, "_run_scope_pids", lambda task_id, run_id: [])
    monkeypatch.setattr(custody, "TERM_GRACE_SECONDS", 1.0)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


@pytest.fixture
def procs():
    started: list[subprocess.Popen] = []

    def spawn(code: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                                stdout=subprocess.PIPE, text=True)
        started.append(proc)
        return proc

    yield spawn
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


SLEEPER = "import time; print('up', flush=True); time.sleep(600)"
STUBBORN = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); "
            "time.sleep(600)")


def _ready(proc: subprocess.Popen) -> int:
    assert proc.stdout.readline().strip() == "up"  # handlers installed before we signal it
    return proc.pid


def _checkpoint(board, *entries: dict) -> None:
    from pathlib import Path
    import os
    (Path(os.environ["HERMES_HOME"]) / "processes.json").write_text(json.dumps(list(entries)))


def _entry(pid: int, *, persist: bool, command: str) -> dict:
    return {"session_id": f"proc_{pid}", "command": command, "pid": pid, "pid_scope": "host",
            "host_start_time": custody._start_ticks(pid), "parent_session_id": SESSION,
            "persist_on_release": persist, "systemd_unit": ""}


def _running_card(board, *, max_runtime=None) -> str:
    tid = kb.create_task(board, title="ship PR #2946", assignee="dev", max_runtime_seconds=max_runtime)
    assert kb.claim_task(board, tid) is not None
    run_id = kb.get_task(board, tid).current_run_id
    with kb.write_txn(board):
        kb._append_event(board, tid, "owner_admitted", {"session_id": SESSION}, run_id=run_id)
    return tid


def _requeue(board, tid) -> dict:
    assert kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)
    assert kb.get_task(board, tid).status == "ready"
    (event,) = [e for e in kb.list_events(board, tid) if e.kind == "reclaimed"]
    return event.payload


def _claim_from(board, monkeypatch, tid, host) -> sync.ClaimGate:
    monkeypatch.setattr(custody, "own_host", lambda conn: host)
    sync.pop_claim_deferral(tid)
    return sync.claim_gate(board, tid, int(time.time()) + 900)


def _wait_dead(pid: int, start: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not custody._alive(pid, start):
            return True
        time.sleep(0.05)
    return False


def test_live_persisted_job_pins_the_retry_to_its_host(board, procs, monkeypatch):
    """Test 1: another host's claim is deferred naming host, pid and command; the same host claims."""
    tid = _running_card(board)
    ship = _ready(procs(SLEEPER))
    _checkpoint(board, _entry(ship, persist=True, command="hermes-core-wt ship 2946"))
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    payload = _requeue(board, tid)
    assert payload["custody"]["host"] == HOST
    assert [p["pid"] for p in payload["custody"]["processes"]] == [ship]

    gate = _claim_from(board, monkeypatch, tid, OTHER)
    assert gate.deferred
    reason = str(sync.pop_claim_deferral(tid))
    assert HOST in reason and f"pid {ship}" in reason and "hermes-core-wt ship 2946" in reason

    assert not _claim_from(board, monkeypatch, tid, HOST).deferred
    assert custody._alive(ship, custody._start_ticks(ship))  # the persisted job was left running


def test_exit_of_the_persisted_job_returns_the_card_to_the_pool(board, procs, monkeypatch):
    """Test 2: once the job exited, the custody host's sweep releases it and a pool claim is allowed."""
    tid = _running_card(board)
    proc = procs(SLEEPER)
    ship = _ready(proc)
    _checkpoint(board, _entry(ship, persist=True, command="ship"))
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    _requeue(board, tid)
    assert custody.sweep(board) == []  # still alive: custody holds
    assert _claim_from(board, monkeypatch, tid, OTHER).deferred

    proc.kill()
    proc.wait(timeout=5)
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    assert custody.sweep(board) == [tid]
    (released,) = [e for e in kb.list_events(board, tid) if e.kind == custody.RELEASED_EVENT]
    assert released.payload["outcome"] == "exited"
    assert not _claim_from(board, monkeypatch, tid, OTHER).deferred


def test_bound_terminates_the_persisted_jobs_then_releases(board, procs, monkeypatch):
    """Test 3: at max_runtime_seconds the jobs are terminated as a group (TERM, then KILL), then a pool claim
    is allowed. The job ignores TERM, so the KILL step is exercised."""
    tid = _running_card(board, max_runtime=120)
    ship = _ready(procs(STUBBORN))
    start = custody._start_ticks(ship)
    _checkpoint(board, _entry(ship, persist=True, command="ship"))
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    payload = _requeue(board, tid)
    until = payload["custody"]["until"]
    assert until - time.time() == pytest.approx(120, abs=5)

    assert custody.sweep(board, now=until - 1) == []
    assert custody._alive(ship, start)
    assert custody.sweep(board, now=until + 1) == [tid]
    assert _wait_dead(ship, start)
    (released,) = [e for e in kb.list_events(board, tid) if e.kind == custody.RELEASED_EVENT]
    assert released.payload == {"key": payload["custody"]["run_uid"] or str(payload["custody"]["run_id"]),
                                "host": HOST, "run_id": payload["custody"]["run_id"], "outcome": "terminated",
                                "terminated": [ship]}
    assert not _claim_from(board, monkeypatch, tid, OTHER).deferred


def test_requeue_without_persisted_jobs_is_unchanged(board, monkeypatch):
    """Test 4: no live persisted job: no custody, and any host may claim at once."""
    tid = _running_card(board)
    _checkpoint(board, {"pid": 999999, "host_start_time": 1, "parent_session_id": SESSION,
                        "persist_on_release": True, "command": "gone"})  # exited long ago
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    payload = _requeue(board, tid)
    assert "custody" not in payload
    assert not _claim_from(board, monkeypatch, tid, OTHER).deferred


def test_plain_command_ignoring_term_is_dead_before_another_host_claims(board, procs, monkeypatch):
    """Test 5: a plain-background or foreground command whose child ignores the first TERM. The requeue ends
    it (TERM, then KILL) before the card is released, so another host's claim is not held by it."""
    tid = _running_card(board)
    stubborn = _ready(procs(STUBBORN))  # a foreground command's child in the run's scope
    background = _ready(procs(STUBBORN))  # a plain background job from the checkpoint
    starts = {p: custody._start_ticks(p) for p in (stubborn, background)}
    _checkpoint(board, _entry(background, persist=False, command="plain ship"))
    monkeypatch.setattr(custody, "_run_scope_pids", lambda task_id, run_id: [stubborn])
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    payload = _requeue(board, tid)
    assert all(not custody._alive(p, s) for p, s in starts.items())  # dead before the release
    assert "custody" not in payload
    assert not _claim_from(board, monkeypatch, tid, OTHER).deferred


def test_plain_kill_spares_the_persisted_job_and_its_children(board, procs, monkeypatch):
    """A scope pid that descends from a persisted job (the job's own children) is not terminated."""
    tid = _running_card(board)
    ship = _ready(procs(SLEEPER))
    _checkpoint(board, _entry(ship, persist=True, command="ship"))
    monkeypatch.setattr(custody, "_run_scope_pids", lambda task_id, run_id: [ship])
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    payload = _requeue(board, tid)
    assert custody._alive(ship, custody._start_ticks(ship))
    assert payload["custody"]["terminated_plain"] == []


def test_other_reclaim_reasons_do_not_touch_processes(board, procs, monkeypatch):
    tid = _running_card(board)
    ship = _ready(procs(SLEEPER))
    _checkpoint(board, _entry(ship, persist=False, command="ship"))
    assert kb.reclaim_task(board, tid, reason="operator", signal_fn=lambda *_: None)
    assert custody._alive(ship, custody._start_ticks(ship))
    (event,) = [e for e in kb.list_events(board, tid) if e.kind == "reclaimed"]
    assert "custody" not in event.payload


def test_custody_of_a_silent_host_stops_holding_after_the_grace(board, procs, monkeypatch):
    tid = _running_card(board, max_runtime=60)
    ship = _ready(procs(SLEEPER))
    _checkpoint(board, _entry(ship, persist=True, command="ship"))
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    until = _requeue(board, tid)["custody"]["until"]
    monkeypatch.setattr(custody, "own_host", lambda conn: OTHER)
    row = board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert custody.claim_refusal(board, row, now=until + custody.RELEASE_GRACE_SECONDS - 1)
    assert custody.claim_refusal(board, row, now=until + custody.RELEASE_GRACE_SECONDS) is None


def test_bound_spares_the_job_while_the_custody_hosts_retry_runs(isolate_usage_accounting, board, procs, monkeypatch):
    """Review of 478094ef: the bound must not kill a persist_on_release job the same host's re-claimed retry
    runs with (another host can't claim a running card, so killing prevents nothing). Once that run ends
    while the job still lives, the bound applies again."""
    tid = _running_card(board, max_runtime=60)
    ship = _ready(procs(STUBBORN))
    start = custody._start_ticks(ship)
    assert start is not None
    _checkpoint(board, _entry(ship, persist=True, command="ship"))
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    until = _requeue(board, tid)["custody"]["until"]
    assert not _claim_from(board, monkeypatch, tid, HOST).deferred
    assert kb.claim_task(board, tid) is not None  # the custody host's own retry runs

    assert custody.sweep(board, now=until + 1) == []
    task = kb.get_task(board, tid)
    assert task is not None and task.status == "running" and custody._alive(ship, start)

    assert kb.reclaim_task(board, tid, reason="operator", signal_fn=lambda *_: None)  # that run ends
    assert custody.sweep(board, now=until + 2) == [tid]
    assert _wait_dead(ship, start)


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("bound_offset", [-1, 1])
def test_terminal_card_releases_custody_without_signalling_job(
        isolate_usage_accounting,
        board, procs, monkeypatch, status, bound_offset):
    tid = _running_card(board, max_runtime=60)
    ship = _ready(procs(SLEEPER))
    start = custody._start_ticks(ship)
    assert start is not None
    _checkpoint(board, _entry(ship, persist=True, command="ship"))
    monkeypatch.setattr(custody, "own_host", lambda conn: HOST)
    until = _requeue(board, tid)["custody"]["until"]
    assert kb.claim_task(board, tid) is not None
    if status == "done":
        assert kb.complete_task(board, tid, summary="finished", fire_lifecycle_hook=False)
    else:
        assert kb.archive_task(board, tid, signal_fn=lambda *_: None)
    assert kb.get_task(board, tid).status == status
    assert custody._alive(ship, start)

    signalled = []
    def terminate_spy(processes):
        signalled.extend(processes)
        return custody.terminate(processes)

    assert custody.sweep(board, now=until + bound_offset, terminate_fn=terminate_spy) == [tid]
    assert signalled == []
    assert custody._alive(ship, start)
    (released,) = [e for e in kb.list_events(board, tid) if e.kind == custody.RELEASED_EVENT]
    assert released.payload["outcome"] == "terminal"
    assert released.payload["status"] == status
    assert custody.sweep(board, now=until + 2, terminate_fn=terminate_spy) == []
    assert signalled == [] and custody._alive(ship, start)


def test_liveness_without_proc_uses_the_registry_fingerprint(procs, monkeypatch):
    """Review nit of 478094ef: on macOS (no /proc) every pid read as dead, so no custody was ever recorded.
    Without /proc the fingerprint is the process registry's own (psutil), matching checkpoint entries."""
    from gateway.status import get_process_start_time
    monkeypatch.setattr(custody, "_HAS_PROC", False)
    proc = procs(SLEEPER)
    pid = _ready(proc)
    assert custody._start_ticks(pid) is not None and custody._start_ticks(pid) == get_process_start_time(pid)
    assert custody._alive(pid, get_process_start_time(pid))
    proc.kill()
    wait_until(lambda: custody._start_ticks(pid) is None, timeout=5, what="killed child reads as dead (zombie)")
    proc.wait(timeout=5)
    assert custody._start_ticks(pid) is None
