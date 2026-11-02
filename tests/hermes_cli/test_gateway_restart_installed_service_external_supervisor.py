"""``hermes gateway restart --detach`` with a canonical service definition on disk that does not own
the gateway, while the running gateway declares ``gateway run --external-supervisor``.

``_cmd_restart`` dispatched to the installed service (``_installed_service_kind_for`` only checks
that the plist/unit FILE exists) before it looked at who supervises the live gateway. When that
service call failed it printed "Gateway service restart failed" and exited 1 — the external-supervisor
hand-back below it was unreachable. Desktop's stale-owner restart (``gateway restart --detach``) then
logged exit 1 and attached to the stale gateway.

Reproduced against a real process: a child whose live argv carries ``gateway run
--external-supervisor`` (read back through psutil by ``_capture_gateway_argv``), a real SIGUSR1 sent
by ``_graceful_restart_via_sigusr1``, and a stubbed supervisor that relaunches a second child once
the first exits. Nothing here touches a live gateway: the orphan reap, launchctl and the pidfile
lookup are stubbed to the two children this test owns.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading

import pytest

from gateway import status as gateway_status
from hermes_cli import gateway as gw
from hermes_cli import gateway_supervised_restart as supervised
from hermes_cli.subcommands.gateway import build_gateway_parser

# Each test spawns and reaps its own `gateway run --external-supervisor` lookalike (a sleeping python -c).
pytestmark = [
    pytest.mark.spawns_gateway_lookalike,
    pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="the handback is a POSIX SIGUSR1 drain"),
]

# The child exits 75 on SIGUSR1 the way a draining gateway exits back to its supervisor.
_FAKE_GATEWAY_CODE = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGUSR1, lambda *a: sys.exit(75))\n"  # windows-footgun: ok — skipped without SIGUSR1
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


def _parse(*argv):
    parser = argparse.ArgumentParser()
    build_gateway_parser(
        parser.add_subparsers(dest="command"),
        cmd_gateway=lambda a: None, cmd_proxy=lambda a: None, cmd_gateway_enroll=lambda a: None,
    )
    return parser.parse_args(["gateway", *argv])


def _spawn_fake_gateway(script) -> subprocess.Popen:
    # `python <tmp>/bin/hermes gateway run --external-supervisor`: the shape the gateway matcher accepts.
    proc = subprocess.Popen(
        [sys.executable, str(script), "gateway", "run", "--external-supervisor"],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    # Reap on exit so the pid really disappears (a zombie still "exists" to _wait_for_pid_exit).
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


class _FakeSupervisor:
    """A KeepAlive manager this test owns: relaunches the gateway once its first instance exits.

    ``close`` stops relaunching before it kills anything: killing ``first`` wakes the watcher, and
    without the closed flag (checked under the same lock the relaunch holds) it spawned ``second``
    after teardown had already passed it, leaking a lookalike gateway past the test.
    """

    def __init__(self, script, relaunch: bool = True):
        self._script = script
        self._lock = threading.Lock()
        self._closed = False
        self.first = _spawn_fake_gateway(script)
        self.second: subprocess.Popen | None = None
        self._relaunch = relaunch
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self):
        self.first.wait()
        with self._lock:
            if self._relaunch and not self._closed:
                self.second = _spawn_fake_gateway(self._script)

    def running_pid(self, *a, **k):
        if self.first.poll() is None:
            return self.first.pid
        return self.second.pid if self.second is not None else None

    def close(self):
        with self._lock:  # waits out an in-flight relaunch, then forbids any later one
            self._closed = True
        for proc in (self.first, self.second):
            if proc is not None and proc.poll() is None:
                proc.kill()
        self._watcher.join(timeout=10)
        for proc in (self.first, self.second):
            if proc is not None:
                proc.wait(timeout=10)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    plist = tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"
    plist.parent.mkdir()
    plist.write_text("<plist/>", encoding="utf-8")  # the stub canonical definition: present on disk, not the owner

    calls = {"service": [], "stopped": False, "started": False, "replacement_started": False}

    def _failing_service_call(kind, verb, system=False):
        calls["service"].append((kind, verb))
        raise subprocess.CalledProcessError(5, ["launchctl", "kickstart", "-k", "gui/501/ai.hermes.gateway"])

    # Refusal guards that would read the real environment; each is asserted to still run below.
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_reap_unsupervised_gateway_orphans", lambda *a, **k: 0)  # never scan real processes
    # Stub service discovery, not the host OS; no real launchctl is ever called.
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda *a: "launchd")
    monkeypatch.setattr(gw, "_systemd_unit_installed", lambda *a, **k: False)
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "_launchd_print_service_pid", lambda domain, label: (False, None))  # label not loaded
    monkeypatch.setattr(gw, "_service_call", _failing_service_call)
    # The handback's own budgets, shortened.
    monkeypatch.setattr(gw, "_get_restart_exit_wait_budget", lambda: 10.0)
    monkeypatch.setattr(supervised, "SUPERVISED_REPLACEMENT_VERIFY_TIMEOUT", 5.0)
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda home, **k: None)
    monkeypatch.setattr("hermes_cli.main_dashboard._get_systemd_service_for_pid", lambda pid: None)
    # The manual fallback must never be reached for a supervised gateway.
    monkeypatch.setattr(gw, "stop_profile_gateway", lambda: calls.__setitem__("stopped", True) or True)
    monkeypatch.setattr(gw, "run_gateway", lambda **k: calls.__setitem__("started", True))
    monkeypatch.setattr(supervised, "start_restart_replacement",
                        lambda *a, **k: calls.__setitem__("replacement_started", True))

    script = tmp_path / "bin" / "hermes"
    script.parent.mkdir()
    script.write_text(_FAKE_GATEWAY_CODE, encoding="utf-8")
    supervisor = _FakeSupervisor(script)
    monkeypatch.setattr(gateway_status, "get_running_pid", supervisor.running_pid)
    yield supervisor, calls
    supervisor.close()


def test_detach_restart_hands_back_to_external_supervisor_despite_failing_installed_service(scenario, capsys):
    supervisor, calls = scenario
    old_pid = supervisor.first.pid
    # Preconditions: the real argv reader sees the marker, and the installed launchd stub is detected.
    assert gw.argv_declares_external_supervisor(gw._capture_gateway_argv(old_pid))
    assert gw._installed_service_kind_for(lambda: False) == "launchd"

    gw.gateway_command(_parse("restart", "--detach"))  # SystemExit(1) before the fix

    out = capsys.readouterr().out
    assert supervisor.first.returncode == 75, "the gateway must be drained via SIGUSR1 back to its supervisor"
    assert supervisor.second is not None
    assert f"relaunched by its supervisor (PID {supervisor.second.pid})" in out
    assert "Gateway service restart failed" not in out
    assert calls["service"] == [], "an externally-supervised gateway is handed back, never service-dispatched"
    assert not (calls["stopped"] or calls["started"] or calls["replacement_started"])


def test_installed_service_owner_also_gets_graceful_handback(scenario, monkeypatch, capsys):
    supervisor, calls = scenario
    # Even the canonical supervisor must drain gracefully, not kickstart its live worker.
    monkeypatch.setattr(gw, "_launchd_print_service_pid", lambda domain, label: (True, os.getpid()))
    gw.gateway_command(_parse("restart", "--detach"))
    assert calls["service"] == []
    assert supervisor.first.returncode == 75
    assert supervisor.second is not None
    assert "relaunched by its supervisor" in capsys.readouterr().out


def test_refusal_guards_still_run_before_the_handback(scenario, monkeypatch):
    supervisor, calls = scenario

    def _refuse(verb, args):
        raise SystemExit(4)

    def unexpected_handback(*args):
        pytest.fail("refused restart reached handback")

    monkeypatch.setattr(supervised, "restart_externally_supervised_gateway", unexpected_handback)
    monkeypatch.setattr("hermes_cli.gateway_lifecycle_guards.refuse_while_sessions_live", _refuse)
    with pytest.raises(SystemExit) as exc:
        gw.gateway_command(_parse("restart", "--detach"))
    assert exc.value.code == 4
    assert supervisor.first.poll() is None, "a refused restart must not signal the gateway"
    assert calls["service"] == []


def test_supervisor_that_never_relaunches_still_fails_loudly(scenario, monkeypatch, capsys):
    supervisor, calls = scenario
    supervisor._relaunch = False
    with pytest.raises(SystemExit) as exc:
        gw.gateway_command(_parse("restart", "--detach"))
    assert exc.value.code == 1
    assert supervisor.first.returncode == 75
    assert "Supervisor did not relaunch the gateway" in capsys.readouterr().out
    assert not (calls["stopped"] or calls["started"] or calls["replacement_started"])


def test_fake_supervisor_close_never_relaunches_after_teardown(tmp_path):
    # Teardown kills `first` while it still runs, which wakes the relaunch watcher. A relaunch
    # after close() returned would leak a `gateway run --external-supervisor` lookalike.
    script = tmp_path / "bin" / "hermes"
    script.parent.mkdir()
    script.write_text(_FAKE_GATEWAY_CODE, encoding="utf-8")
    supervisor = _FakeSupervisor(script)
    first_pid = supervisor.first.pid
    supervisor.close()
    supervisor._watcher.join(timeout=10)
    assert supervisor.second is None, "close() must stop the supervisor before it kills the gateway"
    assert supervisor.first.returncode is not None
    assert not supervisor._watcher.is_alive()
    assert supervisor.running_pid() is None
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)
