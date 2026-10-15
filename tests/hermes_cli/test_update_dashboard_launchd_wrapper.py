"""``hermes update`` must hand a launchd-owned dashboard back to its job even when the job's plist runs a
wrapper script, and must capture a runpy-bootstrapped backend's argv exactly.

The mini's ``com.daveey.hermes-redde-serve`` job runs ``~/.bb-hermes/bin/redde-serve``, which ``exec``s
``hermes dashboard …``. ``_loaded_launchd_backend_jobs`` skips that plist (its ProgramArguments is not a
dashboard command line), so the update replayed the backend's argv detached. That argv came from
``ps -o command=`` + shlex, which split the ``Hermes Python.app`` path and the ``-c`` source, so the
respawn died with a SyntaxError, the runtime was booked ``failed`` and the receipt kept a
``gateway_restart`` follow-up, while launchd had already respawned the dashboard itself.
"""

import sys
import types
from unittest.mock import patch

import pytest

from hermes_cli import dashboard_procs, main_dashboard

_PY = "/Users/me/Applications/Hermes Python.app/Contents/MacOS/python3.14"
_SRC = (
    "import sys, runpy; sys.path.insert(0, '/Users/me/code/hermes-agent'); "
    "sys.argv = ['/Users/me/code/hermes-agent/.venv/bin/hermes', 'dashboard', '--port', '9119', '--no-open']; "
    "runpy.run_path('/Users/me/code/hermes-agent/.venv/bin/hermes', run_name='__main__')"
)
ARGV = [_PY, "-I", "-c", _SRC]
LABEL = "com.example.redde-serve"


def _fake_psutil(argv=None, env=None):
    proc = types.SimpleNamespace(cmdline=lambda: list(argv or []), environ=lambda: dict(env or {}))
    return types.SimpleNamespace(Process=lambda pid: proc, pid_exists=lambda pid: False)


@pytest.mark.platforms("macos")
def test_cmdline_capture_keeps_exact_argv_of_spaced_runpy_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(argv=ARGV))
    with patch("subprocess.run", side_effect=AssertionError("ps must not be needed")):
        assert main_dashboard._dashboard_cmdline_for_pid(4242) == ARGV


@pytest.mark.platforms("macos")
@pytest.mark.parametrize(
    ("env", "print_result", "ancestors", "expected"),
    [
        ({"XPC_SERVICE_NAME": LABEL}, (True, 4242), (), True),
        ({"XPC_SERVICE_NAME": LABEL}, (True, 77), (77, 1), True),  # exec-less wrapper: live pid is an ancestor
        ({"XPC_SERVICE_NAME": LABEL}, (True, 999), (), False),  # job's live process is someone else
        ({"XPC_SERVICE_NAME": LABEL}, (False, None), (), False),  # job not loaded
        ({"XPC_SERVICE_NAME": "0"}, (True, 4242), (), False),
        ({}, (True, 4242), (), False),
    ],
)
def test_launchd_job_from_launch_env(monkeypatch, env, print_result, ancestors, expected):
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(env=env))
    with patch("hermes_cli.gateway._launchd_print_service_pid", return_value=print_result):
        job = main_dashboard._launchd_job_from_launch_env(4242, ancestors)
    assert (job is not None) is expected
    if expected:
        assert job[1] == LABEL and job[2] == print_result[1]


@pytest.mark.platforms("macos")
def test_wrapper_script_launchd_dashboard_is_kickstarted_not_respawned(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(argv=ARGV, env={"XPC_SERVICE_NAME": LABEL}))

    def _fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError

    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[4242]), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch("hermes_cli.gateway._launchd_print_service_pid", return_value=(True, 4242)), \
         patch.object(main_dashboard, "_restart_launchd_job", return_value=True) as restart, \
         patch.object(main_dashboard, "_respawn_dashboard_processes", return_value=[]) as respawn, \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=[1]), \
         patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
         patch("os.kill", side_effect=_fake_kill), \
         patch("time.sleep"):
        result = dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)

    respawn.assert_not_called()
    restart.assert_called_once()
    assert restart.call_args[0][1] == LABEL
    assert result["killed"] == [4242] and result["unrecovered"] == []


@pytest.mark.platforms("macos")
def test_terminal_launched_dashboard_still_respawns_from_argv(monkeypatch):
    """Control: a manual backend (XPC name of the launching terminal app, whose live pid is not
    the backend) keeps the argv respawn."""
    monkeypatch.setitem(
        sys.modules, "psutil", _fake_psutil(argv=ARGV, env={"XPC_SERVICE_NAME": "application.com.example.term.1"})
    )

    def _fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError

    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[4242]), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch("hermes_cli.gateway._launchd_print_service_pid", return_value=(True, 555)), \
         patch.object(main_dashboard, "_restart_launchd_job", return_value=True) as restart, \
         patch.object(main_dashboard, "_respawn_dashboard_processes", return_value=[]) as respawn, \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=[1]), \
         patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
         patch("os.kill", side_effect=_fake_kill), \
         patch("time.sleep"):
        dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)

    restart.assert_not_called()
    respawn.assert_called_once_with([ARGV])


