"""A gateway restart must not make live kanban workers look crashed.

Observed on macOS: the dispatcher stamps each worker with ``"<epoch>|<start>"`` where ``<start>`` is
``psutil.Process.create_time()`` in centiseconds. That value moves when ``kern.boottime`` is adjusted
(sleep/wake, clock sync). The dispatcher in the restarted gateway then read every live worker's start
time 1.00 s off the spawn-time stamp, and an exact string compare called them all "recycled", so dead.
Sixteen live workers were booked ``crashed`` in one tick. Because there were three or more identical
errors, the breaker treated it as systemic and blocked the cards. A respawn then started a duplicate
worker next to each original.
"""

import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _running_task(conn, pid: int) -> str:
    """A task claimed long ago whose live worker ``pid`` was fingerprinted at spawn."""
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3 * 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
    return tid


def _restart_gateway_with_start_time_drift(monkeypatch, drift_centis: int) -> None:
    """Model the restarted gateway's dispatcher: it reads start times shifted by ``drift_centis``."""
    from gateway import status

    real = status._get_process_start_time
    def drifted(pid):
        value = real(pid)
        return None if value is None else value + drift_centis

    monkeypatch.setattr(status, "_get_process_start_time", drifted)
    kbd._recent_worker_exits.clear()  # a new process has reaped nothing


def test_gateway_restart_leaves_live_workers_running_and_unblocked(board, monkeypatch):
    conn = board
    pid = os.getpid()  # alive for the whole test
    tids = [_running_task(conn, pid) for _ in range(3)]  # 3+ identical errors would trip as systemic
    stamped = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tids[0],)).fetchone()[0]
    assert isinstance(stamped, str) and "|" in stamped

    _restart_gateway_with_start_time_drift(monkeypatch, drift_centis=100)  # the observed 1.00 s

    assert kbd._worker_alive(pid, stamped) is True
    assert kbd.detect_crashed_workers(conn) == []
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", []) == []
    for tid in tids:
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.worker_pid == pid
        kinds = {e.kind for e in kb.list_events(conn, tid)}
        assert not kinds & {"crashed", "gave_up", "blocked"}

    # A full dispatcher tick in the restarted gateway: no crash, no respawn of a duplicate.
    spawned = []
    res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
    assert res.crashed == [] and res.auto_blocked == []
    assert spawned == []
    assert all(kb.get_task(conn, t).status == "running" for t in tids)


def test_gateway_restart_still_reclaims_a_really_dead_worker(board, monkeypatch):
    """Control: tolerance must not keep a claim alive for a worker that actually exited."""
    conn = board
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    tid = _running_task(conn, child.pid)
    child.wait()
    _restart_gateway_with_start_time_drift(monkeypatch, drift_centis=100)

    assert tid in kbd.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).worker_pid is None


def test_pid_recycled_beyond_drift_tolerance_is_still_foreign(board, monkeypatch):
    """Control: a live PID whose start time differs by far more than clock drift is a stranger."""
    conn = board
    pid = os.getpid()
    tid = _running_task(conn, pid)
    stamped = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    _restart_gateway_with_start_time_drift(monkeypatch, drift_centis=60 * 100)  # a minute apart

    assert kbd._worker_alive(pid, stamped) is False
    assert tid in kbd.detect_crashed_workers(conn)
