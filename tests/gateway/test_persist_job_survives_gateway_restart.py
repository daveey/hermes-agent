"""A persist_on_release job a Kanban worker started outlives the worker and gateway restarts. [daveey]

Hub incident (2026-11-03): paid GPU-rental watchdogs that Kanban workers started with
``terminal(background=true, persist_on_release=true)`` died with the next gateway restart.
The worker exited (or was requeued by a restart), the next gateway adopted the orphaned job
from ``processes.json`` at startup, and that gateway's NEXT shutdown swept it with
``kill_all(source="gateway_shutdown")``, which deliberately reaches persisted jobs.

These tests drive the real path with real processes: a separate "worker" interpreter spawns
the job through ``ProcessRegistry.spawn_local`` and exits; a fresh registry adopts it the way
gateway startup does; then the gateway's own shutdown sweep runs.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tools.process_registry import ProcessRegistry, ProcessSession

REPO = Path(__file__).resolve().parents[2]

# Real signals to the jobs these tests spawn: the worker child exits, so its job is reparented to
# init and the conftest guard would refuse (and the swallowing shutdown sweep hide) every kill.
pytestmark = [pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups"),
              pytest.mark.live_system_guard_bypass]


@pytest.fixture(autouse=True)
def _no_systemd_scopes(monkeypatch):
    """Plain process groups here; the scope placement has its own test (no real units in CI)."""
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: False)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie is dead for our purposes
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2] != "Z"
    except (OSError, ValueError):
        return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture()
def checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "processes.json"
    monkeypatch.setattr("tools.process_registry.CHECKPOINT_PATH", path)
    return path


@pytest.fixture()
def reap():
    pids: list[int] = []
    yield pids.append
    for pid in pids:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass


def _worker_spawns_persisted_job(checkpoint: Path, tmp_path: Path) -> dict:
    """A separate interpreter (the Kanban worker) starts ``sleep`` persist_on_release, then exits."""
    script = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(REPO)!r})
        from unittest.mock import patch
        from pathlib import Path
        with patch("tools.process_registry.CHECKPOINT_PATH", Path({str(checkpoint)!r})), \
                patch("tools.process_registry._systemd_run_user_scope_available", lambda: False):
            from tools.process_registry import ProcessRegistry
            reg = ProcessRegistry()
            s = reg.spawn_local("sleep 120", cwd={str(tmp_path)!r}, task_id="t_card",
                                owner_task_id="t_card", persist_on_release=True)
            reg._write_checkpoint()
            print(json.dumps({{"pid": s.pid, "id": s.id}}))
        # exits without any cleanup sweep, like a worker the restart requeued
    """)
    env = {**os.environ, "HERMES_KANBAN_TASK": "t_card", "HERMES_HOME": str(tmp_path / "home")}
    env.pop("_HERMES_GATEWAY", None)
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                         timeout=60, check=True, stdin=subprocess.DEVNULL)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _gateway_adopts(checkpoint: Path, job_id: str) -> ProcessRegistry:
    gateway_registry = ProcessRegistry()
    assert gateway_registry.recover_from_checkpoint(owner="gateway") == 1
    assert gateway_registry._running[job_id].detached
    assert gateway_registry._running[job_id].persist_on_release
    return gateway_registry


@pytest.mark.parametrize("restarting", [True, False], ids=["restart", "stop"])
def test_worker_started_persisted_job_survives_gateway_shutdown_sweep(
        checkpoint, tmp_path, reap, monkeypatch, restarting):
    job = _worker_spawns_persisted_job(checkpoint, tmp_path)
    reap(job["pid"])
    assert _alive(job["pid"]), "the worker's persisted job outlived the worker"

    gateway_registry = _gateway_adopts(checkpoint, job["id"])
    monkeypatch.setattr("tools.process_registry.process_registry", gateway_registry)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])

    # The gateway's real shutdown sweep, both phases it runs (a plain stop uses the default).
    kwargs = {"restarting": True} if restarting else {}
    GatewayShutdownMixin._stop_kill_tool_subprocesses("post-interrupt", **kwargs)
    GatewayShutdownMixin._stop_kill_tool_subprocesses("final-cleanup", **kwargs)

    assert not _wait_dead(job["pid"], timeout=1.0), "gateway shutdown killed a worker's persisted job"
    # Still in the checkpoint, so the next gateway adopts it again.
    assert job["id"] in {e["session_id"] for e in json.loads(checkpoint.read_text())}


def _own_job(registry: ProcessRegistry, tmp_path: Path, *, persist: bool) -> ProcessSession:
    s = registry.spawn_local("sleep 120", cwd=str(tmp_path), task_id="gw", owner_task_id="gw",
                             persist_on_release=persist)
    assert s.pid
    return s


def test_restart_spares_gateway_own_persisted_job_but_kills_volatile(checkpoint, tmp_path, reap, monkeypatch):
    registry = ProcessRegistry()
    persisted, volatile = _own_job(registry, tmp_path, persist=True), _own_job(registry, tmp_path, persist=False)
    reap(persisted.pid)
    reap(volatile.pid)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])

    GatewayShutdownMixin._stop_kill_tool_subprocesses("post-interrupt", restarting=True)

    assert _wait_dead(volatile.pid), "a restart still reaps ordinary background jobs"
    assert not _wait_dead(persisted.pid, timeout=1.0), "a restart is not host exit: persisted job survives"


def test_final_stop_still_kills_gateway_own_persisted_job(checkpoint, tmp_path, reap, monkeypatch):
    """Upstream #41225/#46778 contract kept: a real stop reaps the jobs this gateway itself started."""
    registry = ProcessRegistry()
    persisted = _own_job(registry, tmp_path, persist=True)
    reap(persisted.pid)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])

    GatewayShutdownMixin._stop_kill_tool_subprocesses("post-interrupt")

    assert _wait_dead(persisted.pid)


def test_explicit_kill_still_reaches_adopted_persisted_job(checkpoint, tmp_path, reap):
    job = _worker_spawns_persisted_job(checkpoint, tmp_path)
    reap(job["pid"])
    gateway_registry = _gateway_adopts(checkpoint, job["id"])
    gateway_registry.kill_process(job["id"], source="process_manage")
    assert _wait_dead(job["pid"])
