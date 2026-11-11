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
def test_gateway_job_is_a_launchd_osascript_child_that_reads_as_unsupervised(monkeypatch, tmp_path):
    monkeypatch.setenv(runtime.ALLOW_IN_TESTS_ENV, "1")  # this test exists to exercise real launchd
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
    try:
        assert wait_until(lambda: job.poll() is not None, timeout=30)
        assert job.poll() == 0
    finally:  # leave the developer's GUI domain as we found it
        subprocess.run(["launchctl", "remove", job.target.rsplit("/", 1)[-1]], capture_output=True, check=False,
                       stdin=subprocess.DEVNULL)


def _job_program_arguments(monkeypatch, tmp_path, command):
    """ProgramArguments of the plist start_gateway_job hands to ``launchctl bootstrap``."""
    seen = {}

    def fake_launchctl(*args, timeout):
        if args[0] == "bootstrap":
            seen["plist"] = plistlib.loads(Path(args[2]).read_bytes())
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

    monkeypatch.setattr(runtime, "_launchctl", fake_launchctl)
    monkeypatch.setenv(runtime.ALLOW_IN_TESTS_ENV, "1")
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


def _recording_launchctl(monkeypatch):
    calls = []

    def launchctl(*args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")
    monkeypatch.setattr(runtime, "_launchctl", launchctl)
    return calls


def test_a_test_process_never_touches_the_real_launchd_domain(monkeypatch, tmp_path):
    """[daveey] e2e ``hermes chat`` children bootstrapped real jobs into gui/<uid>; their teardown exits then
    read as failing LaunchAgents on the developer Mac. Under a test, the caller's plain-child fallback runs."""
    calls = _recording_launchctl(monkeypatch)
    monkeypatch.delenv(runtime.ALLOW_IN_TESTS_ENV, raising=False)
    assert start_gateway_job([sys.executable, "-c", "pass"], env={}, cwd=tmp_path,
                             log_path=tmp_path / "gateway-stdio.log") is None
    assert calls == []  # no bootstrap, and no ``remove`` of somebody else's finished job either
    assert not list(tmp_path.glob(".*.plist"))


def test_the_opt_in_lets_a_test_reach_launchd(monkeypatch, tmp_path):
    calls = _recording_launchctl(monkeypatch)
    monkeypatch.setenv(runtime.ALLOW_IN_TESTS_ENV, "1")
    assert start_gateway_job([sys.executable, "-c", "pass"], env={}, cwd=tmp_path,
                             log_path=tmp_path / "gateway-stdio.log") is not None
    assert [c[0] for c in calls] == ["list", "bootstrap"]


def test_outside_a_test_the_job_is_bootstrapped(monkeypatch, tmp_path):
    import hermes_state_guard
    calls = _recording_launchctl(monkeypatch)
    monkeypatch.delenv(runtime.ALLOW_IN_TESTS_ENV, raising=False)
    monkeypatch.setattr(hermes_state_guard, "_in_test_context", lambda: False)
    assert start_gateway_job([sys.executable, "-c", "pass"], env={}, cwd=tmp_path,
                             log_path=tmp_path / "gateway-stdio.log") is not None
    assert [c[0] for c in calls] == ["list", "bootstrap"]


def test_a_child_with_a_rebuilt_env_still_reads_as_a_test_by_ancestry(tmp_path):
    """The e2e homes pass an allowlisted env (no PYTEST_*, no HERMES_TEST_ISOLATION): ancestry must hold."""
    root = str(Path(runtime.__file__).resolve().parents[1])
    log = str(tmp_path / "gateway-stdio.log")
    probe = (
        f"import subprocess, sys, pathlib; sys.path.insert(0, {root!r})\n"
        "from hermes_cli import gateway_launchd_runtime as r\n"
        "calls = []\n"
        "r._launchctl = lambda *a, timeout: calls.append(a) or subprocess.CompletedProcess(a, 0, '', '')\n"
        f"job = r.start_gateway_job([sys.executable, '-c', 'pass'], env={{}}, cwd={str(tmp_path)!r}, "
        f"log_path=pathlib.Path({log!r}))\n"
        "print(job is None, calls)\n"
    )
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST") and k not in ("HERMES_TEST_ISOLATION", runtime.ALLOW_IN_TESTS_ENV)}
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, encoding="utf-8",
                         check=False, stdin=subprocess.DEVNULL, timeout=60)
    assert out.stdout.strip() == "True []", out.stdout + out.stderr


def test_a_bare_interpreter_without_the_guard_module_still_starts_the_job(monkeypatch, tmp_path):
    """The restart watcher imports this module from a bare store Python; a missing guard must not strand it."""
    calls = _recording_launchctl(monkeypatch)
    monkeypatch.delenv(runtime.ALLOW_IN_TESTS_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_state_guard", None)  # import -> ImportError
    assert start_gateway_job([sys.executable, "-c", "pass"], env={}, cwd=tmp_path,
                             log_path=tmp_path / "gateway-stdio.log") is not None
    assert [c[0] for c in calls] == ["list", "bootstrap"]
