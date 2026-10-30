"""A running sandbox background job must keep its watcher while the session sits idle. [daveey]

Live, hub alpha-askeve, 2026-10-30: a gateway-owned session started a notify_on_complete job in the
profile's persistent Docker container (proc_eb13cdee12de, owner ``local-c407…``, container key
``profile:alpha-askeve``) and ended its turn. ~7 min later the idle reaper tore the environment down,
the poller's next ``env.execute`` raised, and the session was told the job ended
(``exit_code -1, completion_reason=lost, termination_source=backend_lost``) while it was still
running in the container.

Why: ``_cleanup_inactive_envs`` asks ``has_active_processes(<container key>)``, which matches the RAW
owner id (#120546), never the collapsed container key; the container-key check only ran for
workspace keys. Two guards, one test each:
  1. the reaper keeps any container key that hosts a running background process;
  2. a poller whose environment handle dies anyway re-attaches through the recorded sandbox
     handle and delivers the real exit, and only reports "lost" when the sandbox is really gone.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
import time

import pytest

from tools import terminal_tool as tt
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.terminal_tool_lifecycle import _cleanup_inactive_envs
from tests.waiting import wait_until

CONTAINER_KEY = "profile:alpha-askeve"
OWNER = "local-c407191ced81e5b0977a23a282e835d4e12b0c09bfe247e85912e4b5b783228d"


class _Env:
    """A sandbox env stand-in: runs commands in a local bash until it is cleaned up."""

    def __init__(self, temp_dir: str = ""):
        self.cleaned = False
        self.temp_dir = temp_dir

    def cleanup(self, **_kw):
        self.cleaned = True

    def execute(self, command, cwd="", timeout=10, **_kw):
        if self.cleaned:
            raise RuntimeError("environment was cleaned up")
        # Output through a file: the spawn wrapper's backgrounded job inherits stdout, and a pipe
        # would keep subprocess.run waiting for the job itself (docker exec returns at the shell's exit).
        with tempfile.TemporaryFile("w+") as out:
            res = subprocess.run(["bash", "-c", command], stdout=out, stderr=subprocess.STDOUT, text=True,
                                 timeout=timeout, check=False)
            out.seek(0)
            return {"output": out.read(), "returncode": res.returncode}

    def get_temp_dir(self):
        return self.temp_dir


@pytest.fixture()
def registry(monkeypatch):
    reg = ProcessRegistry()
    monkeypatch.setattr("tools.process_registry.process_registry", reg)
    return reg


@pytest.fixture()
def envs(monkeypatch):
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_creation_locks", {})
    monkeypatch.setattr(tt, "_calls_in_flight", {})
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    return tt._active_environments


def test_reaper_keeps_a_profile_container_env_while_its_background_job_runs(registry, envs):
    env = _Env()
    envs[CONTAINER_KEY] = env
    tt._last_activity[CONTAINER_KEY] = time.time() - 600
    session = ProcessSession(id="proc_eb13", command="sleep 600", task_id=CONTAINER_KEY, owner_task_id=OWNER,
                             pid=43086, pid_scope="sandbox", env_ref=env, started_at=time.time())
    registry._running[session.id] = session

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert not env.cleaned, "the idle reaper tore down the env of a running background job"
    assert envs.get(CONTAINER_KEY) is env

    # Once the job is gone the env is idle again and is reaped as before.
    session.exited = True
    tt._last_activity[CONTAINER_KEY] = time.time() - 600
    _cleanup_inactive_envs(lifetime_seconds=300)
    assert env.cleaned


def _spawn(registry, env, tmp_path, script):
    env.temp_dir = str(tmp_path)
    session = registry.spawn_via_env(env, script, task_id=CONTAINER_KEY, owner_task_id=OWNER,
                                     session_key=f"agent:alpha-askeve:local:dm:{OWNER}")
    session.notify_on_complete = True
    return session


def _wait_exit(session, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not session.exited and time.monotonic() < deadline:
        time.sleep(0.2)
    assert session.exited, "the poller never settled the job"


def test_poller_whose_env_dies_reattaches_and_delivers_the_real_exit(registry, tmp_path, monkeypatch, caplog):
    env = _Env()
    go = tmp_path / "go"
    session = _spawn(registry, env, tmp_path,
                     f"while [ ! -f {shlex.quote(str(go))} ]; do sleep 0.2; done; echo RESTART-PROOF-DONE; exit 3")
    reattached = _Env(str(tmp_path))
    handles = []
    monkeypatch.setattr("tools.environments.reattach.reattach_environment",
                        lambda handle: handles.append(handle) or reattached)
    session.sandbox_handle = {"backend": "docker", "container_id": "214b16909172", "docker_exe": "docker"}

    caplog.set_level("INFO", logger="tools.process_registry")
    env.cleaned = True  # the env handle dies under the poller (idle reap, gateway teardown, ...)
    # The next poll fails on the dead handle; the poller must re-attach, not settle the job.
    wait_until(lambda: handles or session.exited, timeout=15, what="the poller reacting to its dead env")
    assert not session.exited, f"declared {session.completion_reason!r} while the job still runs"
    go.touch()
    _wait_exit(session)

    assert handles == [session.sandbox_handle]
    assert session.exit_code == 3 and session.completion_reason == "exited"
    assert "RESTART-PROOF-DONE" in session.output_buffer
    assert any("proc_" in r.getMessage() and "re-attached" in r.getMessage().lower() and OWNER in r.getMessage()
               for r in caplog.records)


def test_poller_reports_lost_only_when_the_sandbox_cannot_be_reattached(registry, tmp_path, monkeypatch):
    env = _Env()
    session = _spawn(registry, env, tmp_path, "sleep 30")
    session.sandbox_handle = {"backend": "docker", "container_id": "gone", "docker_exe": "docker"}
    monkeypatch.setattr("tools.environments.reattach.reattach_environment", lambda handle: None)

    env.cleaned = True
    _wait_exit(session)
    assert session.completion_reason == "lost" and session.termination_source == "backend_lost"
