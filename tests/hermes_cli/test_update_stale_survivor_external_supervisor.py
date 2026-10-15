"""A stale gateway that declares an external supervisor is handed back, not "stopped".

``hermes update``'s restart phase can miss a gateway (its process matcher found no PID), and the
verify phase then SIGUSR1s the proven-stale survivor. A gateway launched as ``gateway run
--external-supervisor`` (a custom launchd KeepAlive agent) exits 75 and its supervisor relaunches
it on the new code. Counting it as a manual gateway "stopped without a successor" armed an owed
restart that a later CLI start retried, so every update restarted the gateway twice and the
update reported "Restart manually: hermes gateway run" for a gateway that was already back.
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys

import pytest

from hermes_cli.update_cmd_fleet import _GatewayRestartOutcome
from hermes_cli.update_inventory import _restart_mechanism, describe_restart_mechanism


def _outcome(pid: int) -> _GatewayRestartOutcome:
    return _GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[pid], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
    )


@contextlib.contextmanager
def _sigusr1_child():
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, sys, time\n"
         "signal.signal(signal.SIGUSR1, lambda *_: sys.exit(75))\n"
         "print('ready', flush=True)\n"
         "time.sleep(120)"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        yield child
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


def _signal(child, monkeypatch, declares_external: bool):
    from hermes_cli import gateway_supervised_restart
    from hermes_cli.update_cmd_stale_survivors import signal_stale_fleet_survivors

    monkeypatch.setattr(gateway_supervised_restart, "gateway_declares_external_supervisor",
                        lambda pid, home=None: declares_external and pid == child.pid)
    restart = _outcome(child.pid)
    fleet = [{"profile": "default", "pid": child.pid, "state": "stale", "code_sha": "a" * 40}]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        signalled = signal_stale_fleet_survivors(fleet, restart, 10.0)
    assert child.wait(timeout=15) == 75
    return restart, signalled, out.getvalue()


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX drain-first restart signal")
def test_externally_supervised_survivor_is_handed_back_not_owed(monkeypatch):
    with _sigusr1_child() as child:
        restart, signalled, output = _signal(child, monkeypatch, declares_external=True)

    assert signalled == [child.pid]
    assert restart.killed_pids == {child.pid}
    assert restart.stopped_unmapped_pids == set()
    assert restart.externally_supervised_profiles == ["default"]
    assert "Restart manually" not in output


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX drain-first restart signal")
def test_unsupervised_survivor_is_still_owed_a_manual_restart(monkeypatch):
    with _sigusr1_child() as child:
        restart, _signalled, output = _signal(child, monkeypatch, declares_external=False)

    assert restart.stopped_unmapped_pids == {child.pid}
    assert restart.externally_supervised_profiles == []
    assert "Restart manually" in output


def test_self_declared_external_supervisor_plans_a_hand_back_not_manual():
    mechanism = _restart_mechanism("external", "default")
    assert mechanism != "manual"
    assert "supervisor" in describe_restart_mechanism(mechanism, "default")
