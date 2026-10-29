"""Invariant: a run's ``hermes-worker-kanban-<task>-run-<n>.scope`` is stopped once
the run has ended and its worker is gone, so nothing the worker left behind (an
orphaned pytest, ``ssh-agent -s``) keeps running in it for hours or days.
A job the worker started with ``persist_on_release`` survives: its scope gets the
other processes signalled instead of a unit stop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_scope as kws
from tests.waiting import wait_until

# The orphans are this test's own processes, but they are reparented out of its
# subtree (that is the bug), so the live-system kill guard must let them be signalled.
pytestmark = pytest.mark.live_system_guard_bypass


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kws, "_last_scope_sweep", {})
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


class FakeSystemd:
    """``systemctl --user`` for transient scopes whose cgroup.procs live under tmp_path."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path / "cgroup"
        self.units: dict[str, list[int]] = {}
        self.calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(kws, "_CGROUP_ROOT", self.root)
        monkeypatch.setattr(kws, "_systemctl", self)

    def add(self, unit: str, pids: list[int]) -> None:
        self.units[unit] = pids
        d = self.root / "app.slice" / unit
        d.mkdir(parents=True, exist_ok=True)
        (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))

    def __call__(self, *args: str):
        self.calls.append(args)
        out = ""
        if args[0] == "list-units":
            out = "".join(f"{u} loaded active running scope\n" for u in self.units)
        elif args[0] == "show":
            out = f"/app.slice/{args[1]}\n"
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 0, out, "")

    @property
    def stops(self) -> list[str]:
        return [c[-1] for c in self.calls if c[0] == "stop"]


def _orphaning_worker() -> int:
    """Run a fake worker that leaves a sleeping grandchild behind; return the grandchild pid."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import subprocess, sys; p = subprocess.Popen(['sleep', '300'], start_new_session=True, "
         "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)"],
        capture_output=True, text=True, check=True, timeout=30)
    return int(out.stdout.strip())  # the worker has exited; its grandchild still sleeps


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _ended_run(conn, *, ended_ago: int) -> tuple[str, int | None, str]:
    tid = kb.create_task(conn, title="finished", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    kbd._set_worker_pid(conn, tid, _dead_pid())
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True
    conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?", (ended_ago, run_id))
    return tid, run_id, f"hermes-worker-kanban-{tid}-run-{run_id}.scope"


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)  # reap if it is our zombie
    except ChildProcessError:
        pass
    return kws._proc_stat(pid) is not None


def _start(pid: int) -> int:
    stat = kws._proc_stat(pid)
    assert stat is not None
    return stat[1]


def _kill(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_ended_runs_scope_is_stopped_on_dispatch_tick(conn, tmp_path, monkeypatch):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    orphan = _orphaning_worker()
    try:
        tid, run_id, unit = _ended_run(conn, ended_ago=kws.RUN_SCOPE_STOP_GRACE_SECONDS + 60)
        systemd.add(unit, [orphan])

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)

        assert result.stopped_run_scopes == [unit]
        assert systemd.stops == [unit]
        assert ("stop", "--no-block", unit) in systemd.calls
        ev = conn.execute("SELECT payload, run_id FROM task_events WHERE task_id=? AND kind='run_scope_stopped'",
                          (tid,)).fetchone()
        payload = json.loads(ev["payload"])
        assert ev["run_id"] == run_id and payload["mode"] == "stop"
        assert [k["pid"] for k in payload["killed"]] == [orphan] and "sleep 300" in payload["killed"][0]["cmd"]
    finally:
        _kill(orphan)


def test_persisted_job_survives_and_only_the_orphan_is_killed(conn, tmp_path, monkeypatch):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    orphan = _orphaning_worker()
    # A persist_on_release job: a shell wrapper with its own child, both in the scope.
    job = subprocess.Popen(["bash", "-c", "sleep 300 & wait"], start_new_session=True)
    job_child = int(wait_until(lambda: subprocess.run(
        ["pgrep", "-P", str(job.pid)], capture_output=True, text=True, check=False).stdout.split())[0])
    profile_home = tmp_path / ".hermes" / "profiles" / "bb-tuner"
    profile_home.mkdir(parents=True)
    (profile_home / "processes.json").write_text(json.dumps([
        {"session_id": "proc_x", "pid": job.pid, "host_start_time": _start(job.pid),
         "persist_on_release": True},
    ]))
    try:
        _tid, _run_id, unit = _ended_run(conn, ended_ago=kws.RUN_SCOPE_STOP_GRACE_SECONDS + 60)
        systemd.add(unit, [job.pid, job_child, orphan])

        stopped = kws.stop_ended_run_scopes(conn, force=True)

        assert systemd.stops == []  # a unit stop would take the persisted job with it
        assert [r["mode"] for r in stopped] == ["signal"]
        assert sorted(stopped[0]["spared"]) == sorted([job.pid, job_child])
        assert [k["pid"] for k in stopped[0]["killed"]] == [orphan]
        wait_until(lambda: not _alive(orphan), timeout=5, what="orphan killed")
        assert job.poll() is None and _alive(job_child)
    finally:
        _kill(orphan, job_child, job.pid)
        job.wait(timeout=10)


def test_same_pid_without_persisted_marker_is_not_spared(conn, tmp_path, monkeypatch):
    """A recycled pid (start time differs from the checkpoint) is not a persisted job."""
    systemd = FakeSystemd(tmp_path, monkeypatch)
    orphan = _orphaning_worker()
    (tmp_path / ".hermes" / "processes.json").write_text(json.dumps([
        {"pid": orphan, "host_start_time": _start(orphan) - 1, "persist_on_release": True},
    ]))
    try:
        _tid, _run_id, unit = _ended_run(conn, ended_ago=kws.RUN_SCOPE_STOP_GRACE_SECONDS + 60)
        systemd.add(unit, [orphan])
        assert [r["mode"] for r in kws.stop_ended_run_scopes(conn, force=True)] == ["stop"]
        assert systemd.stops == [unit]
    finally:
        _kill(orphan)


def test_scope_is_left_alone_inside_grace_while_running_or_on_another_board(conn, tmp_path, monkeypatch):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    _tid, _run, recent = _ended_run(conn, ended_ago=10)
    live = kb.create_task(conn, title="live", assignee="coder")
    kb.claim_task(conn, live, claimer=kb._claimer_id())
    live_unit = f"hermes-worker-kanban-{live}-run-{kb._current_run_id(conn, live)}.scope"
    foreign = "hermes-worker-kanban-t_elsewhere-run-1.scope"  # a run of another board's DB
    for unit in (recent, live_unit, foreign):
        systemd.add(unit, [os.getppid()])

    assert kws.stop_ended_run_scopes(conn, force=True) == []
    assert systemd.stops == []


def test_scope_with_live_retained_worker_waits_for_the_worker_reaper(conn, tmp_path, monkeypatch):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    worker = subprocess.Popen(["sleep", "300"])
    try:
        tid = kb.create_task(conn, title="finished", assignee="coder")
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        run_id = kb._current_run_id(conn, tid)
        kbd._set_worker_pid(conn, tid, worker.pid)
        assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True
        conn.execute("UPDATE task_runs SET ended_at = ended_at - 3600 WHERE id=?", (run_id,))
        systemd.add(f"hermes-worker-kanban-{tid}-run-{run_id}.scope", [worker.pid])

        assert kws.stop_ended_run_scopes(conn, force=True) == []
        assert systemd.stops == []
    finally:
        worker.kill()
        worker.wait()


def test_sweep_is_throttled_and_never_raises(conn, tmp_path, monkeypatch):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    orphan = _orphaning_worker()
    try:
        _tid, _run, unit = _ended_run(conn, ended_ago=kws.RUN_SCOPE_STOP_GRACE_SECONDS + 60)
        systemd.add(unit, [orphan])
        assert len(kws.stop_ended_run_scopes(conn)) == 1
        assert kws.stop_ended_run_scopes(conn) == []  # within the sweep interval
        assert len(systemd.stops) == 1

        def broken(*args):
            raise RuntimeError("bus gone")
        monkeypatch.setattr(kws, "_systemctl", broken)
        assert kws.stop_ended_run_scopes(conn, force=True) == []
    finally:
        _kill(orphan)
