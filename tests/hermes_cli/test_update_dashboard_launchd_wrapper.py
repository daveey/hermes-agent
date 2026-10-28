"""``hermes update`` must hand a launchd-owned dashboard back to its job even when the job's plist runs a
wrapper script, and must capture a runpy-bootstrapped backend's argv exactly.

The mini's ``com.daveey.hermes-redde-serve`` job runs ``~/.bb-hermes/bin/redde-serve``, which ``exec``s
``hermes dashboard …``. ``_loaded_launchd_backend_jobs`` skips that plist (its ProgramArguments is not a
dashboard command line), so the update replayed the backend's argv detached. That argv came from
``ps -o command=`` + shlex, which split the ``Hermes Python.app`` path and the ``-c`` source, so the
respawn died with a SyntaxError, the runtime was booked ``failed`` and the receipt kept a
``gateway_restart`` follow-up, while launchd had already respawned the dashboard itself.
"""

import os
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
        # A LaunchServices app job (Desktop / terminal) is an ancestor of every child it spawns,
        # but it is no backend supervisor: never attribute the child to it.
        ({"XPC_SERVICE_NAME": "application.com.nousresearch.hermes.1.2"}, (True, 77), (77, 1), False),
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



_DESKTOP_SERVE = [_PY, "-I", "-c", _SRC.replace("'dashboard', '--port', '9119', '--no-open'",
                                                "'serve', '--host', '127.0.0.1', '--port', '0'")]
_APP_LABEL = "application.com.nousresearch.hermes.834263643.834268344"
_APP_PID = 23501


@pytest.mark.platforms("macos")
def test_desktop_local_serve_is_not_booked_on_the_desktop_app_job(monkeypatch):
    """David-MBP 2026-10-28 04:37 PDT: the update stopped Desktop's own ``serve --port 0`` (it inherits
    the app's ``XPC_SERVICE_NAME`` and the app is its parent), attributed it to the Desktop app's
    LaunchServices job, waited for launchd to report a fresh APP pid, booked the backend unrecovered
    and owed ``gateway_restart`` -- while Desktop respawned its backend a second later. The backend
    is Desktop's to bring back: no kickstart of the app job, no argv respawn, nothing unrecovered."""
    monkeypatch.setitem(
        sys.modules, "psutil", _fake_psutil(argv=_DESKTOP_SERVE, env={"XPC_SERVICE_NAME": _APP_LABEL})
    )

    def _fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError

    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[38035]), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch("hermes_cli.gateway._launchd_print_service_pid", return_value=(True, _APP_PID)), \
         patch.object(main_dashboard, "_restart_launchd_job", return_value=False) as restart, \
         patch.object(main_dashboard, "_respawn_dashboard_processes", return_value=[]) as respawn, \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=[_APP_PID, 1]), \
         patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
         patch("os.kill", side_effect=_fake_kill), \
         patch("time.sleep"):
        result = dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)

    restart.assert_not_called()
    respawn.assert_not_called()  # a Desktop --port 0 backend is never resurrected from argv
    assert result["killed"] == [38035] and result["unrecovered"] == []


@pytest.mark.platforms("macos")
def test_real_launchd_backend_job_that_never_returns_stays_unrecovered(monkeypatch):
    """Control: a genuine backend job (non-``application.`` label) whose kickstart never yields a
    fresh pid is still booked unrecovered, so the update keeps warning."""
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(argv=ARGV, env={"XPC_SERVICE_NAME": LABEL}))

    def _fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError

    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[4242]), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch("hermes_cli.gateway._launchd_print_service_pid", return_value=(True, 77)), \
         patch.object(main_dashboard, "_restart_launchd_job", return_value=False), \
         patch.object(main_dashboard, "_respawn_dashboard_processes", return_value=[]), \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=[77, 1]), \
         patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
         patch("os.kill", side_effect=_fake_kill), \
         patch("time.sleep"):
        result = dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)

    assert result["unrecovered"] == [4242]


def _desktop_bootstrap(port: str) -> list[str]:
    """The ``Hermes Python.app -I -I -c`` shape Hermes Desktop spawns its local backend with (live
    pid 87773 on David-MBP): argv assigned inside the source, then the published launcher exec'd."""
    launcher = (
        "import os, re, sys\\nsys.path.insert(0, '/Users/me/.hermes/hermes-agent')\\n"
        "from hermes_cli.main import main\\nsys.argv[0] = re.sub(r'(-script\\\\.pyw|\\\\.exe)?$', '', sys.argv[0])\\n"
        "sys.exit(main())\\n"
    )
    src = (
        "import sys, runpy; sys.path.insert(0, '/Users/me/.hermes/hermes-agent'); "
        f"sys.argv = ['-c', 'serve', '--host', '127.0.0.1', '--port', '{port}']; "
        f'exec("{launcher}")'
    )
    return [_PY, "-I", "-I", "-c", src]


@pytest.mark.parametrize(("port", "expected"), [("0", True), ("9119", False)])
def test_port_zero_check_reads_through_the_desktop_inline_bootstrap(port, expected):
    assert dashboard_procs._is_ephemeral_port_zero_backend(_desktop_bootstrap(port)) is expected


def test_killed_desktop_bootstrap_backend_is_never_respawned():
    assert dashboard_procs._filter_dashboard_respawn_candidates(
        [(87773, _desktop_bootstrap("0"), None)], own_home="/Users/me/.hermes") == []
    # control: a fixed-port backend in the same shape is still replayed
    assert dashboard_procs._filter_dashboard_respawn_candidates(
        [(87774, _desktop_bootstrap("9119"), None)], own_home="/Users/me/.hermes") == [_desktop_bootstrap("9119")]


# --- [daveey] backend supervised by hermes-gateway-launcher (David-MBP 2026-10-28 04:45 PDT) -----------
# launchd job com.daveey.hermes-redde-serve runs ``hermes-gateway-launcher --home ~/.hermes --
# ~/.hermes/bin/redde-serve``; redde-serve execs the dashboard, whose XPC_SERVICE_NAME is ``0`` and whose
# plist ProgramArguments is the launcher. Neither launchd lookup claimed it, so an unfenced update replayed
# its argv detached on :9119 while the launcher (exiting with its child, restarted by KeepAlive) brought its
# own child up: EADDRINUSE or a duplicate unsupervised backend.

_LAUNCHER_PID = 85469
_BACKEND_PID = 87824
_TAIL = ["/Users/me/.hermes/bin/redde-serve"]
_LAUNCHER_ARGV = ["/opt/homebrew/bin/python3", "/Users/me/.hermes/bin/hermes-gateway-launcher",
                  "--home", "/Users/me/.hermes", "--", *_TAIL]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.01)


def _run_launcher_child_update(monkeypatch, successor_after, *, fence_owner=None, ancestors=None,
                               parent_argv=None):
    """Run the update stop/restart against a launcher-child backend. The launcher's successor child
    (pid 99001 under a respawned launcher 99000) appears *successor_after* seconds after the kill, or
    never when None. Returns (result, respawn mock, launchd restart mock, clock)."""
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(argv=ARGV, env={"XPC_SERVICE_NAME": "0"}))
    clock = _Clock()
    killed_at = {}
    parent_argv = parent_argv or _LAUNCHER_ARGV
    ancestors = ancestors if ancestors is not None else [_LAUNCHER_PID, 1]

    def _cmdline(pid):
        if pid == _BACKEND_PID:
            return ARGV
        if pid in (_LAUNCHER_PID, 99000):
            return parent_argv if pid == _LAUNCHER_PID else _LAUNCHER_ARGV
        return None

    def _fake_kill(pid, sig):
        killed_at.setdefault(pid, clock.now)
        if sig == 0:
            raise ProcessLookupError

    def _children(tail, exclude_launchers=frozenset()):
        assert tail == tuple(_TAIL)
        assert _LAUNCHER_PID in exclude_launchers  # the original launcher's children are never successors
        start = killed_at.get(_BACKEND_PID)
        if start is None:
            return [_BACKEND_PID]
        if successor_after is not None and clock.now - start >= successor_after:
            return [99001]
        return []

    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[_BACKEND_PID]), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch.object(main_dashboard, "_dashboard_cmdline_for_pid", side_effect=_cmdline), \
         patch("hermes_cli.gateway._launchd_print_service_pid", return_value=(True, _LAUNCHER_PID)), \
         patch.object(main_dashboard, "_restart_launchd_job", return_value=True) as restart, \
         patch.object(main_dashboard, "_respawn_dashboard_processes", return_value=[]) as respawn, \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=ancestors), \
         patch("hermes_cli.dashboard_procs._posix_descendants", return_value={}), \
         patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
         patch("hermes_cli.dashboard_procs._launcher_children", side_effect=_children, create=True), \
         patch("hermes_cli.update_gateway_fence.ancestor_fence_owner", return_value=fence_owner), \
         patch("os.kill", side_effect=_fake_kill), \
         patch("time.monotonic", side_effect=clock.monotonic), \
         patch("time.sleep", side_effect=clock.sleep):
        result = dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)
    return result, respawn, restart, clock


@pytest.mark.platforms("macos")
def test_launcher_child_dashboard_is_not_respawned_and_recovers_on_successor(monkeypatch):
    result, respawn, restart, _clock = _run_launcher_child_update(monkeypatch, successor_after=20)
    respawn.assert_not_called()  # an argv replay would sit on :9119 beside the launcher's own child
    restart.assert_not_called()
    assert result["killed"] == [_BACKEND_PID] and result["unrecovered"] == []


@pytest.mark.platforms("macos")
def test_launcher_child_that_never_returns_stays_unrecovered(monkeypatch):
    """Control: no successor child within the bounded wait is booked unrecovered (the update keeps
    warning), still without an argv respawn."""
    result, respawn, _restart, clock = _run_launcher_child_update(monkeypatch, successor_after=None)
    respawn.assert_not_called()
    assert result["unrecovered"] == [_BACKEND_PID]
    assert clock.now - 1000.0 <= dashboard_procs._LAUNCHER_SUCCESSOR_TIMEOUT_SECONDS + 15  # bounded


@pytest.mark.platforms("macos")
def test_launcher_child_inside_ancestor_fence_is_deferred_not_waited(monkeypatch):
    """Inside a core-update fence held by an ancestor every launcher is parked until this update exits:
    don't wait for a successor that cannot come, and don't book it unrecovered."""
    result, respawn, _restart, clock = _run_launcher_child_update(
        monkeypatch, successor_after=None, fence_owner=4321)
    respawn.assert_not_called()
    assert result["unrecovered"] == []
    assert clock.now - 1000.0 < 5  # no successor wait


@pytest.mark.platforms("macos")
def test_launcher_tree_backend_behind_a_non_shell_parent_is_not_claimed(monkeypatch):
    """Control: a dashboard some long-lived process (a gateway) spawned under a launcher-supervised tree
    does not make the launcher respawn anything, so it keeps the argv respawn."""
    gateway_pid = 70000
    gateway_argv = [_PY, "-I", "-c", "gateway run"]

    def _ancestors_cmdline(pid):
        return {gateway_pid: gateway_argv, _LAUNCHER_PID: _LAUNCHER_ARGV}.get(pid, ARGV)

    with patch.object(main_dashboard, "_dashboard_cmdline_for_pid", side_effect=_ancestors_cmdline), \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=[gateway_pid, _LAUNCHER_PID, 1]):
        assert dashboard_procs._launcher_supervisor(_BACKEND_PID) is None
    # through an exec-less shell wrapper the launcher still owns it
    with patch.object(main_dashboard, "_dashboard_cmdline_for_pid",
                      side_effect=lambda pid: {gateway_pid: ["/bin/sh", "-c", "x"]}.get(pid, _LAUNCHER_ARGV)), \
         patch("hermes_cli.dashboard_procs._process_ancestors", return_value=[gateway_pid, _LAUNCHER_PID, 1]):
        assert dashboard_procs._launcher_supervisor(_BACKEND_PID) == (_LAUNCHER_PID, tuple(_TAIL))


def test_launcher_children_reads_the_process_table(monkeypatch):
    """``_launcher_children`` matches launchers by their exact supervised command and returns their kids."""
    table = "\n".join([
        f"  {_LAUNCHER_PID}     1 python3 /x/hermes-gateway-launcher --home /h -- {_TAIL[0]}",
        f"  {_BACKEND_PID} {_LAUNCHER_PID} python3.14 -I -c dashboard",
        "  500     1 python3 /x/hermes-gateway-launcher --home /h -- /x/gateway",
        "  501   500 gateway",
    ])
    argv = {_LAUNCHER_PID: _LAUNCHER_ARGV, 500: [*_LAUNCHER_ARGV[:-1], "/x/gateway"]}
    with patch("subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout=table)), \
         patch.object(main_dashboard, "_dashboard_cmdline_for_pid", side_effect=argv.get):
        assert dashboard_procs._launcher_children(tuple(_TAIL)) == [_BACKEND_PID]


_LAUNCHER_LABEL = "com.daveey.hermes-redde-serve"


def _per_pid_psutil(argv_by_pid, env_by_pid):
    def _proc(pid):
        return types.SimpleNamespace(cmdline=lambda: list(argv_by_pid.get(pid) or []),
                                     environ=lambda: dict(env_by_pid.get(pid) or {}))
    return types.SimpleNamespace(Process=_proc, pid_exists=lambda pid: False)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize(("launcher_label", "expect_bootout"),
                         [(_LAUNCHER_LABEL, True), ("0", False)])
def test_dashboard_stop_of_launcher_child_names_the_launchers_keepalive_job(
        monkeypatch, capsys, launcher_label, expect_bootout):
    """``hermes dashboard --stop`` of a launcher child: the launcher exits with it and launchd KeepAlive
    brings the backend straight back, so the hint must be the launcher job's ``launchctl bootout``, not
    "restart it when you're ready". Control: a launcher run outside launchd (no job) keeps the generic hint."""
    monkeypatch.setitem(sys.modules, "psutil", _per_pid_psutil(
        {_BACKEND_PID: ARGV, _LAUNCHER_PID: _LAUNCHER_ARGV},
        {_BACKEND_PID: {"XPC_SERVICE_NAME": "0"}, _LAUNCHER_PID: {"XPC_SERVICE_NAME": launcher_label}}))
    ancestry = {_BACKEND_PID: [_LAUNCHER_PID, 1], _LAUNCHER_PID: [1]}

    def _fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError

    with patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[_BACKEND_PID]), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch.object(main_dashboard, "_dashboard_cmdline_for_pid",
                      side_effect={_BACKEND_PID: ARGV, _LAUNCHER_PID: _LAUNCHER_ARGV}.get), \
         patch("hermes_cli.gateway._launchd_print_service_pid", return_value=(True, _LAUNCHER_PID)), \
         patch.object(main_dashboard, "_respawn_dashboard_processes") as respawn, \
         patch("hermes_cli.dashboard_procs._process_ancestors", side_effect=lambda pid: ancestry.get(pid, [1])), \
         patch("hermes_cli.dashboard_procs._posix_descendants", return_value={}), \
         patch("os.kill", side_effect=_fake_kill), \
         patch("time.sleep"):
        result = dashboard_procs._kill_stale_dashboard_processes(restart_managed=False)

    out = capsys.readouterr().out
    respawn.assert_not_called()
    assert result["killed"] == [_BACKEND_PID]
    target = f"gui/{os.getuid()}/{_LAUNCHER_LABEL}"
    assert (f"launchctl bootout {target}" in out) is expect_bootout
    assert ("Restart the dashboard when you're ready" in out) is (not expect_bootout)


@pytest.mark.platforms("macos")
def test_surviving_wrapper_shell_under_the_old_launcher_is_not_a_successor(monkeypatch):
    """Exec-less wrapper shape (launcher -> sh -> dashboard): just after the kill the ``sh`` is still the
    OLD launcher's child. It is no successor; only a child of a respawned launcher (new pid) counts."""
    sh_pid = 88000
    old_table = "\n".join([
        f"  {_LAUNCHER_PID}     1 python3 /x/hermes-gateway-launcher --home /h -- {_TAIL[0]}",
        f"  {sh_pid} {_LAUNCHER_PID} /bin/sh {_TAIL[0]}",
    ])
    new_table = "\n".join([
        f"  99000     1 python3 /x/hermes-gateway-launcher --home /h -- {_TAIL[0]}",
        f"  99001 99000 /bin/sh {_TAIL[0]}",
    ])
    clock = _Clock()
    respawn_at = [None]  # None: the old launcher (and its sh) never go away

    def _ps(*_a, **_k):
        table = new_table if respawn_at[0] is not None and clock.now - 1000.0 >= respawn_at[0] else old_table
        return types.SimpleNamespace(returncode=0, stdout=table)

    argv = {_LAUNCHER_PID: _LAUNCHER_ARGV, 99000: _LAUNCHER_ARGV}
    waits = {tuple(_TAIL): [_BACKEND_PID]}
    common = (patch("subprocess.run", side_effect=_ps),
              patch.object(main_dashboard, "_dashboard_cmdline_for_pid", side_effect=argv.get),
              patch("hermes_cli.update_gateway_fence.ancestor_fence_owner", return_value=None),
              patch("time.monotonic", side_effect=clock.monotonic),
              patch("time.sleep", side_effect=clock.sleep))
    with common[0], common[1], common[2], common[3], common[4]:
        assert dashboard_procs._await_launcher_successors(
            waits, {_BACKEND_PID}, {tuple(_TAIL): {_LAUNCHER_PID}}) == [_BACKEND_PID]
        # control: once launchd restarts the launcher (new pid), its fresh child is the successor
        clock.now, respawn_at[0] = 1000.0, 17.0
        assert dashboard_procs._await_launcher_successors(
            waits, {_BACKEND_PID}, {tuple(_TAIL): {_LAUNCHER_PID}}) == []
