"""A multiplexed gateway's profiles each recover only their own background jobs. [daveey]

One ``ProcessRegistry`` serves every profile of a multiplexed gateway, and each profile has its own
``processes.json``. Every profile's checkpoint used to receive every live job; when the job finished
only the file of the profile that saw the exit lost it, so each later restart re-attached the finished
job from another profile's stale copy and wrote another result there (proc_05186a9e0419, owned by
alpha-askeve, re-attached by alpha-follow 18:12Z, bb-tuner 19:11Z and ci-sweeper 20:12Z on 2026-10-30).
"""

import contextlib
import json
import shlex
import subprocess
import sys
import time

import pytest

from tools.process_registry import ProcessRegistry

OWNER = "alpha-askeve"
OTHER = "coach"
KEY = f"agent:{OWNER}:slack:dm:C123:1761813563.000100"


class LocalShellSandbox:
    """Stands in for a re-attached sandbox: runs the poller's commands in a local bash."""

    def execute(self, command: str, cwd: str = "", timeout: int = 10, **_kw) -> dict:
        res = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout, check=False)
        return {"output": res.stdout + res.stderr, "returncode": res.returncode}


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _start_wrapped(temp_dir, sid: str, script: str) -> int:
    """Start *script* exactly as ``spawn_via_env`` wraps it inside a sandbox; return its PID."""
    q = shlex.quote
    log, pid, ext = (f"{temp_dir}/hermes_bg_{sid}.{e}" for e in ("log", "pid", "exit"))
    wrapper = (f"( nohup bash -c {q(script)} > {q(log)} 2>&1; rc=$?; printf '%s\\n' \"$rc\" > {q(ext)} ) "
               f"> /dev/null 2>&1 & echo $! > {q(pid)} && cat {q(pid)}")
    out = subprocess.run(["bash", "-c", wrapper], capture_output=True, text=True, timeout=10, check=False).stdout
    return int(out.strip().splitlines()[-1])


def _entry(sid: str, pid: int, temp_dir, key: str = KEY) -> dict:
    return {
        "session_id": sid, "command": "~/work/qa.sh", "pid": pid, "pid_scope": "sandbox",
        "started_at": time.time(), "task_id": f"profile_{OWNER}", "owner_task_id": key, "session_key": key,
        "notify_on_complete": True, "watcher_platform": "local", "watcher_chat_id": "C123", "watcher_interval": 1,
        "sandbox_handle": {"backend": "docker", "container_id": "214b16909172", "docker_exe": "docker"},
        "sandbox_temp_dir": str(temp_dir), "writer_pid": _dead_pid(), "writer_start_time": None,
    }


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """A Hermes root with two named profiles, as the hub's multiplexed gateway serves them."""
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("")
    out = {"default": root}
    for name in (OWNER, OTHER):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("")
        out[name] = home
    monkeypatch.setenv("HERMES_HOME", str(root))
    sandbox = LocalShellSandbox()
    monkeypatch.setattr("tools.environments.reattach.reattach_environment", lambda handle: sandbox)
    return out


@contextlib.contextmanager
def scope(home):
    """The profile scope the multiplexed gateway runs each profile's recovery and turns under."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _gateway_registry() -> ProcessRegistry:
    reg = ProcessRegistry()
    reg.profile_scoped_checkpoints = True  # what multiplexed gateway startup sets
    return reg


def _ids(home) -> list:
    path = home / "processes.json"
    return [e["session_id"] for e in json.loads(path.read_text())] if path.exists() else []


def _result(home, sid):
    return home / "logs" / "process-results" / f"{sid}.json"


def _wait_exit(reg, sid, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = reg.get(sid)
        if session is not None and session.exited:
            return session
        time.sleep(0.1)
    raise AssertionError(f"{sid} never exited")


def test_stale_copy_in_another_profile_is_not_reattached_after_the_owner_saw_the_exit(homes, tmp_path, caplog):
    sid = "proc_05186a9e0419"
    pid = _start_wrapped(tmp_path, sid, "sleep 1; echo DONE; exit 3")
    entry = _entry(sid, pid, tmp_path)
    # The state the hub was in: both profiles' checkpoints carry the job.
    for name in (OWNER, OTHER):
        (homes[name] / "processes.json").write_text(json.dumps([entry]))

    owner_gw = _gateway_registry()
    with scope(homes[OWNER]):
        assert owner_gw.recover_from_checkpoint(owner="gateway") == 1
        assert _wait_exit(owner_gw, sid).exit_code == 3
    assert _result(homes[OWNER], sid).exists()
    owner_gw.pending_watchers.clear()

    # The next gateway restart recovers the other profile.
    caplog.set_level("INFO", logger="tools.process_registry")
    restarted = _gateway_registry()
    with scope(homes[OTHER]):
        assert restarted.recover_from_checkpoint(owner="gateway") == 0
    assert sid not in restarted._running and not restarted.pending_watchers
    assert not any("Re-attached sandbox process" in r.getMessage() for r in caplog.records)
    assert not _result(homes[OTHER], sid).exists()
    assert sid not in _ids(homes[OTHER]) and sid not in _ids(homes[OWNER])


def test_finished_job_left_in_another_profile_is_dropped_at_startup(homes, tmp_path):
    """The one-time cleanup: the hub's ci-sweeper/coach copies of a job whose result is recorded."""
    from tools.process_registry_profiles import consolidate_checkpoints

    sid = "proc_05186a9e0419"
    (homes[OTHER] / "processes.json").write_text(json.dumps([_entry(sid, 51438, tmp_path)]))
    _result(homes[OWNER], sid).parent.mkdir(parents=True)
    _result(homes[OWNER], sid).write_text("{}")

    assert consolidate_checkpoints(multiplexed=True) == 1
    assert _ids(homes[OTHER]) == [] and _ids(homes[OWNER]) == []


def test_non_owner_profile_never_reattaches_another_profiles_job(homes, tmp_path, caplog):
    sid = "proc_foreign"
    pid = _start_wrapped(tmp_path, sid, "sleep 30")
    try:
        (homes[OTHER] / "processes.json").write_text(json.dumps([_entry(sid, pid, tmp_path)]))
        caplog.set_level("INFO", logger="tools.process_registry")

        other_gw = _gateway_registry()
        with scope(homes[OTHER]):
            assert other_gw.recover_from_checkpoint(owner="gateway") == 0
        assert sid not in other_gw._running and not other_gw.pending_watchers
        assert not any("Re-attached sandbox process" in r.getMessage() for r in caplog.records)
        # The copy moved to its owner's checkpoint, which re-attaches it exactly once.
        assert _ids(homes[OTHER]) == [] and _ids(homes[OWNER]) == [sid]
        with scope(homes[OWNER]):
            assert other_gw.recover_from_checkpoint(owner="gateway") == 1
        assert sid in other_gw._running

        # While it runs, the other profile's checkpoint writes never persist it.
        with scope(homes[OTHER]):
            other_gw._write_checkpoint()
        with scope(homes[OWNER]):
            other_gw._write_checkpoint()
        assert _ids(homes[OTHER]) == [] and _ids(homes[OWNER]) == [sid]
    finally:
        subprocess.run(["bash", "-c", f"kill {pid} 2>/dev/null; pkill -P {pid} 2>/dev/null"], check=False)


def test_single_profile_gateway_keeps_recovering_its_own_main_keyed_jobs(homes, tmp_path):
    """A non-multiplexed gateway of a named profile builds ``agent:main:`` keys for its own sessions."""
    sid = "proc_main_key"
    pid = _start_wrapped(tmp_path, sid, "sleep 30")
    try:
        (homes[OTHER] / "processes.json").write_text(
            json.dumps([_entry(sid, pid, tmp_path, key="agent:main:slack:dm:C123")]))
        reg = ProcessRegistry()  # not multiplexed
        with scope(homes[OTHER]):
            assert reg.recover_from_checkpoint(owner="gateway") == 1
    finally:
        subprocess.run(["bash", "-c", f"kill {pid} 2>/dev/null; pkill -P {pid} 2>/dev/null"], check=False)
