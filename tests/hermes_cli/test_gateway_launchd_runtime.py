"""An unmanaged macOS gateway runs as a one-off launchd job through osascript, not as its caller's child.

Local Network Privacy attributes a process to its responsible app; a gateway left running after the
app that started it quits becomes responsible for itself and macOS refuses its LAN connects (a model
server on another machine answers ``No route to host``). A launchd-spawned osascript stays the
responsible code for the job's whole life (#71206).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import plistlib
from pathlib import Path

from hermes_cli import gateway_launchd_runtime as runtime
from hermes_cli.gateway_launchd_runtime import GatewayJob, responsible_app, start_gateway_job
from tests.waiting import wait_until


def _command_of(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8",
                          check=False, stdin=subprocess.DEVNULL).stdout.strip()


@pytest.mark.platforms("macos")
def test_gateway_job_is_a_launchd_osascript_child_that_reads_as_unsupervised(tmp_path):
    report = tmp_path / "report.json"
    release = tmp_path / "release"
    child = (
        "import json, os, pathlib, time; "
        f"pathlib.Path({str(report)!r}).write_text(json.dumps({{'ppid': os.getppid(), "
        "'xpc': os.environ.get('XPC_SERVICE_NAME'), 'marker': os.environ.get('PROBE_MARKER')})); "
        f"[time.sleep(0.1) for _ in range(300) if not pathlib.Path({str(release)!r}).exists()]"
    )
    job = start_gateway_job([sys.executable, "-c", child], env={**os.environ, "PROBE_MARKER": "env-carried"},
                            cwd=tmp_path, log_path=tmp_path / "gateway-stdio.log")
    assert job is not None, "launchd refused the job in this user's GUI domain"
    try:
        try:
            wait_until(lambda: report.exists() or job.poll() is not None, timeout=30)
        except AssertionError:
            pass  # the assert below names what the job did instead
        log = tmp_path / "gateway-stdio.log"
        state = subprocess.run(["launchctl", "print", job.target], capture_output=True, text=True, encoding="utf-8",
                               check=False, stdin=subprocess.DEVNULL).stdout
        assert report.exists(), (f"tmp={tmp_path}\nlog={log.read_text(encoding='utf-8') if log.exists() else None!r}\n"
                                 f"launchctl print:\n{state[:3000]}")
        seen = json.loads(report.read_text(encoding="utf-8-sig"))
        wrapper = seen["ppid"]
        # The caller is not an ancestor: launchd (pid 1) spawned the wrapper, the wrapper the command. The
        # wrapper is the signed Hermes Python.app when installed (its TCC grants), else osascript.
        expected = responsible_app([sys.executable]) or "/usr/bin/osascript"
        assert _command_of(wrapper).startswith(str(expected)), _command_of(wrapper)
        assert subprocess.run(["ps", "-o", "ppid=", "-p", str(wrapper)], capture_output=True, text=True, encoding="utf-8",
                              check=False, stdin=subprocess.DEVNULL).stdout.strip() == "1"
        assert seen["marker"] == "env-carried"
        # Read as unsupervised, or /restart would exit for a respawn no KeepAlive provides.
        assert seen["xpc"] == "0"
        assert not list(tmp_path.glob(".*.plist")), "the plist holding the launch env must not stay on disk"
        assert job.poll() is None
    finally:
        release.write_text("", encoding="utf-8")
    assert wait_until(lambda: job.poll() is not None, timeout=30)
    assert job.poll() == 0


def _job_program_arguments(monkeypatch, tmp_path, command):
    """ProgramArguments of the plist start_gateway_job hands to ``launchctl bootstrap``."""
    seen = {}

    def fake_launchctl(*args, timeout):
        if args[0] == "bootstrap":
            seen["plist"] = plistlib.loads(Path(args[2]).read_bytes())
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(runtime, "_launchctl", fake_launchctl)
    assert start_gateway_job(command, env={}, cwd=tmp_path, log_path=tmp_path / "gateway-stdio.log") is not None
    return seen["plist"]["ProgramArguments"]


def _fake_signed_app(home: Path) -> Path:
    exe = home / "Applications" / "Hermes Python.app" / "Contents" / "MacOS" / "python3.14"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    return exe


def test_job_is_owned_by_the_signed_python_app_when_the_command_runs_it(monkeypatch, tmp_path):
    # The osascript wrapper made every child's privacy request read as "osascript" (Desktop folder prompt
    # on the mini from an agent's ``find ~``); the signed bundle holds Full Disk Access instead.
    exe = _fake_signed_app(tmp_path)
    args = _job_program_arguments(monkeypatch, tmp_path, [str(exe), "-m", "hermes_cli.main", "gateway", "run"])
    assert args[0] == str(exe), args
    assert "/usr/bin/osascript" not in args


def test_job_is_owned_by_the_signed_twin_of_the_store_python(monkeypatch, tmp_path):
    signed = tmp_path / "signed-python3.14"
    signed.write_text("", encoding="utf-8")
    store = "/x/tools/python-3.14.9+20261021-darwin-arm64/bin/python3"
    monkeypatch.setattr("hermes_cli._launchers.signed_python_app",
                        lambda p: signed if str(p) == store else None)
    args = _job_program_arguments(monkeypatch, tmp_path, [store, "-m", "hermes_cli.main", "gateway", "run"])
    assert args[0] == str(signed), args


def test_job_keeps_the_osascript_wrapper_without_a_signed_app(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli._launchers.signed_python_app", lambda p: None)
    monkeypatch.setattr(runtime.sys, "executable", "/usr/local/bin/python3")
    args = _job_program_arguments(monkeypatch, tmp_path, ["/usr/local/bin/python3", "-m", "hermes_cli.main"])
    assert args[:4] == ["/usr/bin/osascript", "-l", "JavaScript", "-e"], args


def test_a_job_keeps_reporting_its_exit_past_the_spawn_deadline_once_the_ensure_moves_it(monkeypatch):
    """A slow start the ensure keeps waiting on must still surface the job's exit (runtime_exited)."""
    import time
    calls = []

    def launchctl(*args, timeout):
        calls.append(timeout)
        return None if timeout <= 0 else subprocess.CompletedProcess(args, 0, "\tlast exit code = 1\n", "")
    monkeypatch.setattr(runtime, "_launchctl", launchctl)
    job = GatewayJob("gui/501/probe", deadline=time.monotonic() - 1)
    assert job.poll() is None
    job.extend_deadline(time.monotonic() + 60)
    job._checked = 0.0
    assert job.poll() == 1
