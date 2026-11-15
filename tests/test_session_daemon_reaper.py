"""The session teardown stops processes left running against this run's Hermes homes.

A test that runs a real ``hermes chat -q`` child gets a grandchild ``gateway run`` in its own
session; no per-test subprocess patch sees it, and pytest's tmp cleanup removes only directories.
"""
from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import psutil

from tests import conftest as suite_conftest
from tests.waiting import wait_until


def _detached(env_home, marker):
    code = "import time\nwhile True: time.sleep(0.2)\n"
    env = {**os.environ, "HERMES_HOME": str(env_home), "LIVE_REAP_MARKER": marker}
    return subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.platforms("linux", "macos")
@pytest.mark.live_system_guard_bypass  # signals only the two children this test spawned
def test_session_teardown_terminates_daemons_under_the_session_basetemp_only(tmp_path):
    basetemp, elsewhere = tmp_path / "basetemp", tmp_path / "elsewhere"
    (basetemp / "t0" / "hermes").mkdir(parents=True)
    elsewhere.mkdir()
    inside = _detached(basetemp / "t0" / "hermes", "inside")
    outside = _detached(elsewhere, "outside")
    try:
        # Both children have exec'd with their HERMES_HOME (what the reaper reads via psutil environ()).
        wait_until(lambda: {inside.pid, outside.pid} <= set(suite_conftest._processes_with_hermes_home_under(
            [basetemp, elsewhere])), timeout=10, what='both detached children started')
        config = SimpleNamespace(_tmp_path_factory=SimpleNamespace(_basetemp=basetemp))
        reaped = suite_conftest._reap_session_hermes_processes(config)
        assert inside.pid in reaped and outside.pid not in reaped
        assert inside.wait(timeout=5) is not None
        assert outside.poll() is None, "a process outside this session's homes was signalled"
    finally:
        for proc in (inside, outside):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.platforms("linux", "macos")
@pytest.mark.live_system_guard_bypass  # signals nothing: the only child is outside every session root
def test_session_teardown_skips_a_live_gateway_on_the_real_hermes_home_without_touching_it(tmp_path):
    # A host's own gateways are visible in psutil's process environment inventory.
    # The scan must neither signal them nor resolve their home: realpath lstat()s the real home,
    # which the real-home I/O guard (active in this test) refuses.
    assert suite_conftest._REAL_HERMES_ROOT_CANDIDATES, "the real-home guard has no root to refuse"
    real_profile = suite_conftest._REAL_HERMES_ROOT_CANDIDATES[0] / "profiles" / "live-gateway"
    basetemp = tmp_path / "basetemp"
    live = _detached(real_profile, "live")
    try:
        wait_until(lambda: _environ_home(live.pid) == str(real_profile), timeout=10,
                   what="the live-gateway stand-in exec'd with its HERMES_HOME")
        config = SimpleNamespace(_tmp_path_factory=SimpleNamespace(_basetemp=basetemp))
        assert live.pid not in suite_conftest._reap_session_hermes_processes(config)
        assert live.poll() is None, "a process on the real Hermes home was signalled"
    finally:
        if live.poll() is None:
            live.kill()
        live.wait(timeout=5)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("denied", [PermissionError(13, "force permission denied (originated from sysctl(KERN_PROCARGS2) -> errno 0)"), psutil.AccessDenied(123)])
def test_session_scan_continues_after_psutil_wrapped_access_denied(monkeypatch, tmp_path, denied):
    def inaccessible():
        raise SystemError("<built-in function proc_environ> returned a result with an exception set") from denied

    processes = [SimpleNamespace(pid=-1, environ=inaccessible),
                 SimpleNamespace(pid=-2, environ=lambda: {"HERMES_HOME": str(tmp_path / "owned")}),
                 SimpleNamespace(pid=-3, environ=lambda: {"HERMES_HOME": str(tmp_path.parent / "foreign")})]
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(processes))
    assert suite_conftest._processes_with_hermes_home_under([tmp_path]) == [-2]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("cause,message", [
    (None, "<built-in function proc_environ> returned a result with an exception set"),
    (ValueError("bad buffer"), "<built-in function proc_environ> returned a result with an exception set"),
    (PermissionError(13, "unrelated"), "<built-in function proc_environ> returned a result with an exception set"),
    (psutil.AccessDenied(123), "different C extension failure"),
])
def test_session_scan_does_not_hide_other_system_errors(monkeypatch, tmp_path, cause, message):
    error = SystemError(message)

    def broken():
        raise error from cause

    monkeypatch.setattr(psutil, "process_iter", lambda: iter([SimpleNamespace(pid=-1, environ=broken)]))
    with pytest.raises(SystemError) as caught:
        suite_conftest._processes_with_hermes_home_under([tmp_path])
    assert caught.value is error


def _environ_home(pid):
    try:
        return psutil.Process(pid).environ().get("HERMES_HOME")
    except (psutil.Error, OSError):
        return None
