"""Capability-bearing Linux user managers must not taint clean comment ancestry."""
from pathlib import Path
from types import SimpleNamespace
import os
import shutil
import subprocess
import sys

import psutil
import pytest

from hermes_cli import kanban_comment_writer as writer


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case,clean", [
    ("manager", True), ("permission_error", True), ("third_uid", False), ("ordinary", False),
    ("no_extra_caps", False), ("not_user_manager", False),
    ("missing_status", False), ("malformed_caps", False),
    ("marked", False), ("vanished", False), ("argv_name", False),
    ("argv_extra", False), ("missing_caps", False), ("uid_error", False),
])
def test_unreadable_manager_exception_is_narrow(monkeypatch, case, clean):
    uid = os.getuid()
    if uid == 0:
        pytest.skip("requires an unprivileged reader")
    def environ():
        if case == "marked":
            return {"HERMES_AGENT": "1"}
        if case == "vanished":
            raise psutil.NoSuchProcess(4242)
        if case == "permission_error":
            raise PermissionError("ptrace denied")
        raise psutil.AccessDenied(4242)
    def uids():
        if case == "uid_error":
            raise psutil.AccessDenied(4242)
        return (uid + (case == "third_uid"),) * 3
    proc = SimpleNamespace(ppid=lambda: 1, environ=environ, uids=uids)
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    real_stat = os.stat
    monkeypatch.setattr(writer.os, "stat", lambda path, *a, **kw: (
        SimpleNamespace(st_uid=uid) if str(path) == "/proc/4242/environ"
        else real_stat(path, *a, **kw)))
    real_text = Path.read_text
    def text(path, **kwargs):
        if str(path) not in {"/proc/self/status", "/proc/4242/status"}:
            return real_text(path, **kwargs)
        if case == "missing_status":
            raise PermissionError("status unreadable")
        if str(path) == "/proc/self/status":
            return "CapPrm:\t0000000000000001\n"
        caps = "invalid" if case == "malformed_caps" else "1" if case == "no_extra_caps" else "800000000"
        name = "python" if case == "ordinary" else "systemd"
        if case == "missing_caps":
            return f"Name:\t{name}\n"
        return f"Name:\t{name}\nCapPrm:\t{caps}\n"
    monkeypatch.setattr(Path, "read_text", text)
    argv = {
        "not_user_manager": b"/usr/lib/systemd/systemd\0--system\0",
        "argv_name": b"/usr/bin/python\0--user\0",
        "argv_extra": b"/usr/lib/systemd/systemd\0--user\0extra\0",
    }.get(case, b"/usr/lib/systemd/systemd\0--user\0")
    real_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (
        argv if str(path) == "/proc/4242/cmdline" else real_bytes(path)))
    def read(pid):
        return (0, {}) if pid == 1 else writer._read_proc(pid)
    if clean:
        assert writer.agent_ancestor(4242, read=read) is None
    elif case == "marked":
        assert writer.agent_ancestor(4242, read=read) == "4242:HERMES_AGENT"
    else:
        with pytest.raises(writer.Inconclusive):
            writer.agent_ancestor(4242, read=read)


@pytest.mark.platforms("linux")
def test_real_user_manager_ancestry():
    """Read real /proc, including the manager's kernel-denied environment, through init."""
    managers = []
    for proc in psutil.process_iter():
        try:
            if proc.uids().real == os.getuid() and proc.name() == "systemd" and "--user" in proc.cmdline():
                managers.append(proc)
        except psutil.Error:
            continue
    if not managers:
        pytest.skip("no systemd --user")
    for proc in managers:
        assert writer.agent_ancestor(proc.pid) is None


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("marked", [False, True])
def test_real_clean_or_marked_user_unit(marked):
    """A real user unit is detached from pytest's marked ancestry, not a bounded fake walk."""
    from tools.environments.local import build_subprocess_env

    if not all(shutil.which(cmd) for cmd in ("systemctl", "systemd-run")):
        pytest.skip("systemd tools unavailable")
    env = build_subprocess_env(scrub_secrets=True)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
    probe = subprocess.run(["systemctl", "--user", "show-environment"],
                           env=env, capture_output=True, timeout=10, check=False)
    if probe.returncode:
        pytest.skip("user manager bus unavailable")
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from hermes_cli.kanban_comment_writer import agent_ancestor; "
        "print(repr(agent_ancestor()), flush=True)"
    )
    args = ["systemd-run", "--user", "--pipe", "--wait", "--collect", "--quiet",
            "/usr/bin/env", "-i"]
    if marked:
        args.append("HERMES_SUPERVISED_CHILD=1")
    result = subprocess.run(args + [sys.executable, "-c", code, str(Path(__file__).resolve().parents[2])],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    if marked:
        assert ":HERMES_SUPERVISED_CHILD" in result.stdout
    else:
        assert result.stdout.strip() == "None"
