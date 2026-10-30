"""A gateway restart must not silently drop a background process's completion watcher. [daveey]

Before: ``_recover_from_checkpoint_locked`` skipped every ``pid_scope != "host"`` entry
("in-sandbox PIDs mean nothing once the env handle is gone"), and only Desktop/TUI owners got
the "exited while unwatched" wake. A Slack session that ended its turn on "I'll report when
it finishes" was never woken (alpha-askeve, 2026-10-30, pid 7331 in hermes-8278aebf).

Paths covered, each driven through the real gateway process watcher:
  (a) sandbox process, sandbox still up: re-attached; its real exit code and output tail are delivered;
  (b) sandbox gone (no handle / container down / wrapper files missing): a "watcher lost" notice is delivered;
  (c) gateway-owned host process that exited while no gateway watched it: the unknown-exit wake is delivered.
"""

import asyncio
import json
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tools.process_registry import ProcessRegistry
from tools.process_registry_notifications import format_process_notification

GW_KEY = "agent:main:slack:dm:C123:1761813563.000100"
TUI_KEY = "20261030_074804_142b8369"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class LocalShellSandbox:
    """Stands in for a re-attached sandbox: runs the poller's commands in a local bash."""

    def __init__(self):
        self.commands: list[str] = []

    def execute(self, command: str, cwd: str = "", timeout: int = 10, **_kw) -> dict:
        self.commands.append(command)
        res = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout, check=False)
        return {"output": res.stdout + res.stderr, "returncode": res.returncode}


def _start_wrapped(temp_dir, sid: str, script: str) -> int:
    """Start *script* exactly as ``spawn_via_env`` wraps it inside a sandbox; return its PID."""
    q = shlex.quote
    log, pid, ext = (f"{temp_dir}/hermes_bg_{sid}.{e}" for e in ("log", "pid", "exit"))
    wrapper = (f"( nohup bash -c {q(script)} > {q(log)} 2>&1; rc=$?; printf '%s\\n' \"$rc\" > {q(ext)} ) "
               f"> /dev/null 2>&1 & echo $! > {q(pid)} && cat {q(pid)}")
    out = subprocess.run(["bash", "-c", wrapper], capture_output=True, text=True, timeout=10, check=False).stdout
    return int(out.strip().splitlines()[-1])


def _sandbox_entry(sid: str, pid: int, temp_dir, *, key=GW_KEY, platform="slack", handle=None) -> dict:
    return {
        "session_id": sid, "command": "~/work/qa.sh ~/work/wt-main main", "pid": pid, "pid_scope": "sandbox",
        "started_at": time.time(), "task_id": "profile_alpha-askeve", "owner_task_id": key, "session_key": key,
        "notify_on_complete": True, "watcher_platform": platform, "watcher_chat_id": "C123" if platform else "",
        "watcher_interval": 1 if platform else 0,
        "sandbox_handle": {"backend": "docker", "container_id": "abc123", "docker_exe": "docker"}
        if handle is None else handle,
        "sandbox_temp_dir": str(temp_dir), "writer_pid": _dead_pid(), "writer_start_time": None,
    }


@pytest.fixture()
def checkpoint(tmp_path):
    path = tmp_path / "processes.json"
    with patch("tools.process_registry.CHECKPOINT_PATH", path):
        yield path


@pytest.fixture()
def registry(monkeypatch):
    reg = ProcessRegistry()
    monkeypatch.setattr("tools.process_registry.process_registry", reg)
    return reg


def _gateway_runner(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    import gateway.run as gateway_run

    (tmp_path / "config.yaml").write_text("display:\n  background_process_notifications: concise\n")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig())
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    runner._launching_turn_active = AsyncMock(return_value=False)
    return runner


def _run_recovered_watchers(runner, registry, timeout: float = 20.0) -> list[str]:
    """Run every recovered watcher the way gateway startup does; return the injected texts."""
    watchers, registry.pending_watchers = registry.pending_watchers, []
    assert watchers, "recovery queued no watcher: nothing would ever wake the session"

    async def _go():
        await asyncio.wait_for(asyncio.gather(*(runner._watch_process(w) for w in watchers)), timeout)

    asyncio.run(_go())
    return [call.args[0] for call in runner._enqueue_process_completion_notification.await_args_list]


# --------------------------------------------------------------------------- (a)

def test_a_alive_sandbox_process_is_reattached_and_its_completion_delivered(
        checkpoint, registry, tmp_path, monkeypatch, caplog):
    sandbox = LocalShellSandbox()
    pid = _start_wrapped(tmp_path, "proc_qa", "sleep 2; echo 'pytest rc=1'; echo DONE; exit 3")
    checkpoint.write_text(json.dumps([_sandbox_entry("proc_qa", pid, tmp_path)]))
    handles = []
    monkeypatch.setattr("tools.environments.reattach.reattach_environment",
                        lambda handle: handles.append(handle) or sandbox)

    caplog.set_level("INFO", logger="tools.process_registry")
    assert registry.recover_from_checkpoint(owner="gateway") == 1
    assert handles == [{"backend": "docker", "container_id": "abc123", "docker_exe": "docker"}]
    session = registry._running["proc_qa"]
    assert session.env_ref is sandbox and session.pid_scope == "sandbox"
    assert any("Re-attached sandbox process proc_qa" in r.getMessage() and GW_KEY in r.getMessage()
               for r in caplog.records)
    # A re-attached job stays in the checkpoint (with its handle) for the next restart.
    saved = {e["session_id"]: e for e in json.loads(checkpoint.read_text())}
    assert saved["proc_qa"]["sandbox_handle"]["container_id"] == "abc123"

    texts = _run_recovered_watchers(_gateway_runner(monkeypatch, tmp_path), registry)
    assert len(texts) == 1
    assert "Background process proc_qa" in texts[0] and "(exit code 3)" in texts[0]
    assert "DONE" in texts[0]
    assert registry.get("proc_qa").exit_code == 3


def test_a_tui_owned_sandbox_completion_goes_through_the_readopt_sink(checkpoint, registry, tmp_path, monkeypatch):
    sandbox = LocalShellSandbox()
    pid = _start_wrapped(tmp_path, "proc_tui", "echo hi; exit 0")
    checkpoint.write_text(json.dumps([_sandbox_entry("proc_tui", pid, tmp_path, key=TUI_KEY, platform="")]))
    monkeypatch.setattr("tools.environments.reattach.reattach_environment", lambda handle: sandbox)
    delivered = []
    registry.on_unknown_exit = delivered.append

    assert registry.recover_from_checkpoint(owner="tui") == 1
    deadline = time.monotonic() + 15
    while not delivered and time.monotonic() < deadline:
        time.sleep(0.2)
    assert len(delivered) == 1
    evt = delivered[0]
    assert evt["session_key"] == TUI_KEY and evt["exit_code"] == 0 and "hi" in evt["output"]
    assert evt["park_if_unowned"] is True


# --------------------------------------------------------------------------- (b)

@pytest.mark.parametrize("why", ["container_down", "no_handle", "wrapper_files_gone", "container_restarted"])
def test_b_unreachable_sandbox_delivers_watcher_lost_notice(checkpoint, registry, tmp_path, monkeypatch, caplog, why):
    sandbox = LocalShellSandbox()
    if why == "container_restarted":
        _start_wrapped(tmp_path, "proc_lost", "exit 0")  # stale wrapper files survive in the container's /tmp
        sandbox.started_at = time.time() + 60
    entry = _sandbox_entry("proc_lost", 7331, tmp_path, handle={} if why == "no_handle" else None)
    if why == "wrapper_files_gone":
        entry["sandbox_temp_dir"] = str(tmp_path / "nowhere")
    checkpoint.write_text(json.dumps([entry]))
    monkeypatch.setattr("tools.environments.reattach.reattach_environment",
                        lambda handle: None if why == "container_down" else sandbox)

    caplog.set_level("INFO", logger="tools.process_registry")
    registry.recover_from_checkpoint(owner="gateway")
    assert "proc_lost" not in registry._running
    assert any("proc_lost" in r.getMessage() and GW_KEY in r.getMessage() and "watcher lost" in r.getMessage()
               for r in caplog.records)
    # The notice is one-shot: the entry leaves the checkpoint, so the next restart does not repeat it.
    assert [e["session_id"] for e in json.loads(checkpoint.read_text())] == []

    texts = _run_recovered_watchers(_gateway_runner(monkeypatch, tmp_path), registry)
    assert len(texts) == 1
    assert "proc_lost" in texts[0] and "watcher lost" in texts[0].lower()


def test_b_tui_owned_unreachable_sandbox_notice_goes_through_the_readopt_sink(
        checkpoint, registry, tmp_path, monkeypatch):
    checkpoint.write_text(json.dumps([_sandbox_entry("proc_tui_lost", 7331, tmp_path, key=TUI_KEY, platform="")]))
    monkeypatch.setattr("tools.environments.reattach.reattach_environment", lambda handle: None)
    delivered = []
    registry.on_unknown_exit = delivered.append

    registry.recover_from_checkpoint(owner="tui")
    assert len(delivered) == 1
    assert delivered[0]["completion_reason"] == "watcher_lost"
    assert "watcher lost" in (format_process_notification(delivered[0]) or "").lower()


# --------------------------------------------------------------------------- (c)

def _host_entry(sid: str, pid: int) -> dict:
    return {
        "session_id": sid, "command": "bash long_job.sh", "pid": pid, "pid_scope": "host",
        "host_start_time": ProcessRegistry._safe_host_start_time(pid), "started_at": time.time(),
        "task_id": "default", "owner_task_id": GW_KEY, "session_key": GW_KEY, "notify_on_complete": True,
        "watcher_platform": "slack", "watcher_chat_id": "C123", "watcher_interval": 1,
        "writer_pid": _dead_pid(), "writer_start_time": None,
    }


def test_c_gateway_owned_host_process_that_exited_unwatched_wakes_its_session(
        checkpoint, registry, tmp_path, monkeypatch):
    checkpoint.write_text(json.dumps([_host_entry("proc_gone", _dead_pid())]))
    registry.recover_from_checkpoint(owner="gateway")

    texts = _run_recovered_watchers(_gateway_runner(monkeypatch, tmp_path), registry)
    assert len(texts) == 1
    assert "proc_gone" in texts[0] and "status unknown" in texts[0].lower()


def test_c_gateway_readopted_host_process_that_exits_later_wakes_its_session(
        checkpoint, registry, tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    checkpoint.write_text(json.dumps([_host_entry("proc_later", proc.pid)]))
    assert registry.recover_from_checkpoint(owner="gateway") == 1

    def _reap():
        proc.wait()
    import threading
    threading.Thread(target=_reap, daemon=True).start()
    texts = _run_recovered_watchers(_gateway_runner(monkeypatch, tmp_path), registry)
    assert len(texts) == 1
    assert "proc_later" in texts[0] and "status unknown" in texts[0].lower()


# --------------------------------------------------------------------------- plumbing

def test_spawn_via_env_checkpoints_the_reattach_handle_and_temp_dir(checkpoint, registry, tmp_path):
    class Env(LocalShellSandbox):
        def get_temp_dir(self):
            return str(tmp_path)

        def reattach_handle(self):
            return {"backend": "docker", "docker_exe": "docker", "container_id": "c0ffee"}

    session = registry.spawn_via_env(Env(), "sleep 5", task_id="t", session_key=GW_KEY)
    try:
        saved = {e["session_id"]: e for e in json.loads(checkpoint.read_text())}[session.id]
        assert saved["pid_scope"] == "sandbox"
        assert saved["sandbox_handle"] == {"backend": "docker", "docker_exe": "docker", "container_id": "c0ffee"}
        assert saved["sandbox_temp_dir"] == str(tmp_path)
    finally:
        registry.kill_process(session.id)


def _fake_docker(tmp_path, *, running: str, exec_stderr: str = "", exec_rc: int = 0):
    exe = tmp_path / "docker"
    exe.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = inspect ]; then echo {running}; exit 0; fi\n'
        f'if [ "$1" = exec ]; then shift 2; '
        + (f'echo {shlex.quote(exec_stderr)} >&2; exit {exec_rc}; ' if exec_stderr else "")
        + '"$@"; exit $?; fi\nexit 2\n')
    exe.chmod(0o755)
    return str(exe)


def test_reattach_docker_returns_an_exec_handle_only_for_a_running_container(tmp_path):
    from tools.environments.reattach import ReattachedDockerSandbox, reattach_environment

    up = reattach_environment({"backend": "docker", "docker_exe": _fake_docker(tmp_path, running="true"),
                               "container_id": "abc"})
    assert isinstance(up, ReattachedDockerSandbox)
    assert up.execute("echo hi; exit 4") == {"output": "hi\n", "returncode": 4}
    down_dir = tmp_path / "down"
    down_dir.mkdir()
    assert reattach_environment({"backend": "docker", "docker_exe": _fake_docker(down_dir, running="false"),
                                 "container_id": "abc"}) is None
    assert reattach_environment({"backend": "ssh", "host": "x"}) is None
    assert reattach_environment({}) is None


def test_reattached_container_that_vanishes_marks_the_job_lost_not_exited(tmp_path):
    from tools.environments.reattach import ReattachedDockerSandbox, SandboxGoneError

    env = ReattachedDockerSandbox(
        _fake_docker(tmp_path, running="true", exec_stderr="Error: No such container: abc", exec_rc=1), "abc")
    with pytest.raises(SandboxGoneError):
        env.execute("kill -0 1")
    reg = ProcessRegistry()
    from tools.process_registry import ProcessSession
    session = ProcessSession(id="proc_vanish", command="x", pid=7, pid_scope="sandbox", env_ref=env)
    reg._running[session.id] = session
    reg._env_poller_loop(session, env, "/tmp/l", "/tmp/p", "/tmp/e")
    assert session.exited and session.completion_reason == "lost" and session.termination_source == "backend_lost"


def test_another_writer_keeps_sandbox_entries_for_their_owners_recovery(checkpoint, tmp_path):
    """Every Hermes process on the home rewrites processes.json; a sandbox PID cannot be probed, so the
    entry used to vanish on the next foreign write and a restarted gateway had nothing to re-attach."""
    live_writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        mine = _sandbox_entry("proc_live_writer", 7331, tmp_path)
        mine.update(writer_pid=live_writer.pid, writer_start_time=ProcessRegistry._safe_host_start_time(live_writer.pid))
        orphan = _sandbox_entry("proc_orphan", 7332, tmp_path)  # writer dead, awaiting the owner's restart
        stale = {**_sandbox_entry("proc_stale", 7333, tmp_path), "started_at": time.time() - 8 * 24 * 3600}
        checkpoint.write_text(json.dumps([mine, orphan, stale]))
        ProcessRegistry()._write_checkpoint()  # an unrelated process (kanban worker, CLI) writes its table
        kept = {e["session_id"] for e in json.loads(checkpoint.read_text())}
        assert kept == {"proc_live_writer", "proc_orphan"}
    finally:
        live_writer.kill()
        live_writer.wait()


def test_gateway_recovery_leaves_desktop_sandbox_entries_and_settles_its_own(checkpoint, registry, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.environments.reattach.reattach_environment", lambda handle: None)
    checkpoint.write_text(json.dumps([
        _sandbox_entry("proc_gw_lost", 7331, tmp_path),
        _sandbox_entry("proc_tui_job", 7332, tmp_path, key=TUI_KEY, platform=""),
    ]))
    registry.recover_from_checkpoint(owner="gateway")
    assert [e["session_id"] for e in json.loads(checkpoint.read_text())] == ["proc_tui_job"]

    tui = ProcessRegistry()
    delivered = []
    tui.on_unknown_exit = delivered.append
    tui.recover_from_checkpoint(owner="tui")
    assert [e["session_id"] for e in delivered] == ["proc_tui_job"]
    # Settled: the next restart does not repeat the notice.
    assert json.loads(checkpoint.read_text()) == []


def test_docker_level_exec_failure_is_never_read_as_the_job_exiting(tmp_path):
    """A daemon hiccup (or a paused container) makes `docker exec` fail without running the command;
    the poller must skip that tick, not conclude from the error text that the PID is gone."""
    from tools.environments.reattach import ReattachedDockerSandbox

    env = ReattachedDockerSandbox(_fake_docker(
        tmp_path, running="true", exec_stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        exec_rc=1), "abc")
    assert env.execute('kill -0 "$(cat /tmp/p)" 2>/dev/null; echo $?') == {"output": "", "returncode": 125}
