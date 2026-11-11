"""terminal.update_survivable_processes: terminal processes outlive a gateway/backend restart. [daveey]

t_01a1c7d04d9046d2. A hub core update restarts the gateway/backend unit (KillMode=control-group), which killed
every terminal child in it (a background quick_check, 2026-11-10). With the key on, a background job and a long
foreground command run in their own transient scope; the next backend re-attaches them and delivers the
completion to the owning session once. Integration tests run an owner in a real transient user service and stop
it the way systemd stops the gateway (SIGTERM to the whole cgroup).
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import tools.process_registry as pr
import tools.process_registry_survivable as surv
from tests.waiting import wait_until
from tools.interrupt import set_interrupt
from tools.process_registry import ProcessRegistry

REPO = str(Path(pr.__file__).resolve().parents[1])


# ── unit ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def key(monkeypatch):
    """Set the config key (and pretend Linux + a usable scope) without touching config.yaml."""
    state = {"on": True, "scope": True}
    monkeypatch.setattr(pr, "_IS_LINUX", True)
    monkeypatch.setattr(pr.ProcessRegistry, "_config_value",
                        staticmethod(lambda section, k, fallback: state["on"] if k == surv.CONFIG_KEY else fallback))
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: state["scope"])
    monkeypatch.setattr(surv.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(surv, "_warned", False)
    return state


def test_background_jobs_default_to_persist_only_with_the_key_on(key):
    assert surv.default_persist(None, "local") is True
    assert surv.default_persist(False, "local") is False, "an explicit persist_on_release=false must win"
    assert surv.default_persist(True, "local") is True
    assert surv.default_persist(None, "local", pty=True) is False, "a PTY job keeps its PTY"
    assert surv.default_persist(None, "docker") is False
    key["on"] = False
    assert surv.default_persist(None, "local") is False
    assert surv.default_persist(True, "local") is True


def test_key_on_without_a_scope_keeps_todays_behaviour_and_warns_once(key, caplog):
    key["scope"] = False
    with caplog.at_level("WARNING", logger="tools.process_registry"):
        assert surv.default_persist(None, "local") is False
        assert surv.usable() is False
    warnings = [r for r in caplog.records if surv.CONFIG_KEY in r.getMessage()]
    assert len(warnings) == 1


def test_key_off_or_non_linux_is_disabled(key, monkeypatch):
    key["on"] = "false"
    assert surv.enabled() is False
    key["on"] = "true"
    assert surv.enabled() is True
    monkeypatch.setattr(pr, "_IS_LINUX", False)
    assert surv.enabled() is False


def test_foreground_eligibility_needs_local_env_and_a_timeout_above_60s(key):
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment.__new__(LocalEnvironment)
    assert surv.foreground_eligible(env, "local", 61)
    assert not surv.foreground_eligible(env, "local", 60)
    assert not surv.foreground_eligible(env, "local", "x")
    assert not surv.foreground_eligible(object(), "local", 600)
    assert not surv.foreground_eligible(env, "docker", 600)
    key["on"] = False
    assert not surv.foreground_eligible(env, "local", 600)


def test_only_update_and_shutdown_interrupts_are_survivable():
    import threading

    tid = threading.get_ident()
    try:
        for reason in ("hermes update pause", "gateway shutdown", "backend exit"):
            set_interrupt(True, tid, reason=reason)
            assert surv.survivable_interrupt_reason(None, tid) == reason
        for reason in ("user interrupt", "new message", "gateway inactivity watchdog", None):
            set_interrupt(False, tid)
            set_interrupt(True, tid, reason=reason)
            assert surv.survivable_interrupt_reason(tid) is None, reason
    finally:
        set_interrupt(False, tid)


def test_log_tail_drops_the_foreground_wrappers_markers():
    from tools.process_registry_output_log import strip_shell_markers

    text = ("fg-done\n\n__HERMES_PIPE_181265ba766b__0__HERMES_PIPE_181265ba766b__\n\n"
            "__HERMES_CWD_181265ba766b__/home/u__HERMES_CWD_181265ba766b__\n")
    assert strip_shell_markers(text) == "fg-done\n"
    assert strip_shell_markers("plain __HERMES_ text\n") == "plain __HERMES_ text\n"


def test_tee_wrapper_records_the_commands_own_exit_status(tmp_path):
    log = tmp_path / "proc_x.log"
    script = surv.tee_wrapped("echo out; echo err >&2; exit 7", log)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 7
    assert proc.stdout == "out\nerr\n"
    assert log.read_text() == "out\nerr\n"
    assert (tmp_path / "proc_x.exit").read_text().strip() == "7"


def test_held_foreground_command_is_checkpointed_until_released(tmp_path, monkeypatch):
    """A crash mid-wait must leave the command to the next owner: it is in processes.json while held."""
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")
    registry = ProcessRegistry()
    proc = subprocess.Popen(["sleep", "30"])
    try:
        session = registry._new_session("sleep 30", "t", "t", "k", str(tmp_path), persist_on_release=True)
        session.pid, session.host_start_time = proc.pid, registry._safe_host_start_time(proc.pid)
        session.output_log = str(tmp_path / f"{session.id}.log")
        registry.hold_foreground(session)
        entries = json.loads((tmp_path / "processes.json").read_text())
        assert [e["session_id"] for e in entries] == [session.id]
        assert registry.get(session.id) is None, "a held command is not a listed background job"
        registry.release_foreground(session.id)
        assert json.loads((tmp_path / "processes.json").read_text()) == []
    finally:
        proc.kill()
        proc.wait()


# ── integration (real transient user service, stopped like the gateway) ──────


def _user_systemd() -> bool:
    return bool(pr._IS_LINUX and shutil.which("systemd-run") and pr._systemd_run_user_scope_available())


OWNER = """
import json, os, sys, threading, time
sys.path.insert(0, {repo!r})
from tools.interrupt import set_interrupt
from tools.terminal_tool import _handle_terminal
args, reason, out = json.loads({args!r}), {reason!r}, {out!r}
box = {{}}
def run():
    box["result"] = json.loads(_handle_terminal(args, task_id="survive-task"))
t = threading.Thread(target=run)
t.start()
if not args.get("background"):
    time.sleep(3)
    set_interrupt(True, t.ident, reason=reason)
t.join(90)
open(out + ".tmp", "w").write(json.dumps(box.get("result")))
os.replace(out + ".tmp", out)
time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _run_owner(tmp_path, monkeypatch, args: dict, *, key_on: bool, reason=None) -> dict:
    """Start the terminal tool in a transient user service (as the gateway runs), get its tool result, then
    stop the service (SIGTERM to its whole cgroup, KillMode=control-group). Returns the tool result."""
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_PROBED_AT", 0.0)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(f"terminal:\n  update_survivable_processes: {str(key_on).lower()}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    out = tmp_path / "result.json"
    script = tmp_path / "owner.py"
    script.write_text(OWNER.format(repo=REPO, args=json.dumps(args), reason=reason, out=str(out)))
    unit = f"hermes-test-survive-owner-{uuid.uuid4().hex[:8]}.service"
    env = pr.systemd_user_bus_env() or dict(os.environ)
    subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
                    "--property", "KillMode=control-group", f"--setenv=HERMES_HOME={home}",
                    "--setenv=HERMES_SESSION_KEY=20261111_000000_survive",
                    f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
                    sys.executable, str(script)], check=True, timeout=15, env=env)
    try:
        wait_until(out.exists, timeout=90, what="the owner's tool result")
        result = json.loads(out.read_text())
    finally:
        # ``stop`` returns once systemd has SIGTERMed (then SIGKILLed) the unit's whole cgroup.
        subprocess.run(["systemctl", "--user", "stop", unit], check=False, timeout=60, env=env)
    return result


def _cleanup(pid):
    if pid and _alive(pid):
        os.killpg(pid, 9) if os.getpgid(pid) == pid else os.kill(pid, 9)


def _completions(registry: ProcessRegistry, *, settle: float) -> list:
    """Drain the completion queue until one event arrived, then for ``settle`` more seconds (exactly-once)."""
    import queue

    events: list = [registry.completion_queue.get(timeout=60)]
    end = time.monotonic() + settle
    while (left := end - time.monotonic()) > 0:
        try:
            events.append(registry.completion_queue.get(timeout=left))
        except queue.Empty:
            break
    return [e for e in events if e.get("type", "completion") == "completion"]


@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("linux")
@pytest.mark.skipif(not _user_systemd(), reason="needs a user systemd bus")
def test_background_job_survives_owner_stop_and_reattaches_with_one_completion(tmp_path, monkeypatch):
    result = _run_owner(tmp_path, monkeypatch, {"command": "sleep 6; echo survived-done", "background": True,
                                                "notify": True}, key_on=True)
    pid = result["pid"]
    try:
        assert result.get("persist_on_release") is True
        assert _alive(pid), "stopping the owner's unit killed its background job"
        registry = ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
        sid = result["session_id"]
        assert registry.poll(sid)["status"] == "running"
        events = _completions(registry, settle=8)
        assert [(e["session_id"], e["exit_code"]) for e in events] == [(sid, 0)]
        assert "survived-done" in events[0]["output"]
        assert "survived-done" in registry.read_log(sid)["output"]
    finally:
        _cleanup(pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("linux")
@pytest.mark.skipif(not _user_systemd(), reason="needs a user systemd bus")
def test_reattached_job_can_be_killed(tmp_path, monkeypatch):
    result = _run_owner(tmp_path, monkeypatch, {"command": "sleep 300", "background": True, "notify": True},
                        key_on=True)
    pid = result["pid"]
    try:
        assert _alive(pid)
        registry = ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
        assert registry.kill_process(result["session_id"])["status"] in ("killed", "already_exited")
        wait_until(lambda: not _alive(pid), timeout=20, what="the killed job to exit")
    finally:
        _cleanup(pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("linux")
@pytest.mark.skipif(not _user_systemd(), reason="needs a user systemd bus")
def test_key_off_background_job_dies_with_its_owner(tmp_path, monkeypatch):
    result = _run_owner(tmp_path, monkeypatch, {"command": "sleep 60", "background": True, "notify": True},
                        key_on=False)
    pid = result["pid"]
    try:
        assert "persist_on_release" not in result
        wait_until(lambda: not _alive(pid), timeout=20, what="the job to die with its owner (unchanged)")
    finally:
        _cleanup(pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("linux")
@pytest.mark.skipif(not _user_systemd(), reason="needs a user systemd bus")
def test_long_foreground_command_outlives_a_shutdown_interrupt(tmp_path, monkeypatch):
    result = _run_owner(tmp_path, monkeypatch, {"command": "sleep 8; echo fg-done", "timeout": 120},
                        key_on=True, reason="gateway shutdown")
    pid = result.get("pid") or 0
    try:
        sid = result["session_id"]
        assert result["status"] == "continues_in_background"
        assert result["output"].splitlines()[-1] == f"process continues as {sid}"
        assert _alive(pid), "the interrupted foreground command did not survive its owner's stop"
        registry = ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
        events = _completions(registry, settle=5)
        assert [(e["session_id"], e["exit_code"]) for e in events] == [(sid, 0)]
        assert events[0]["output"].strip() == "fg-done"
    finally:
        _cleanup(pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms("linux")
@pytest.mark.skipif(not _user_systemd(), reason="needs a user systemd bus")
def test_user_stop_still_kills_a_long_foreground_command(tmp_path, monkeypatch):
    marker = tmp_path / "ran-past-stop"
    result = _run_owner(tmp_path, monkeypatch, {"command": f"sleep 8; touch {marker}", "timeout": 120},
                        key_on=True, reason=None)
    assert result["exit_code"] == 130
    assert "session_id" not in result
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint() == 0
    with pytest.raises(AssertionError):  # the command would have touched it ~5 s after the stop
        wait_until(marker.exists, timeout=8, what="a command a user Stop should have killed")
