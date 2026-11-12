"""A systemd unit's ``gateway run`` child behind a non-exec wrapper is a service gateway, not a manual one.

hermes-worker-2, 2026-11-12: ``hermes-gateway.service`` runs ``hermes-gateway-broker`` (``token-broker exec
… -- github_quota.py … -- hermes gateway run``), so the unit's ``MainPID`` is the wrapper and the gateway is
its child. ``_get_service_pids`` returned only the MainPID, so the post-update manual sweep mapped the unit's
own gateway to profile ``default`` and armed a detached ``gateway run --replace`` watcher. That relaunch ran in
``hermes-core-update.service``'s cgroup and held the gateway lock, while the unit looped on exit 75
(``already_running``) every 7.5 s. These tests use a real process tree for the wrapper and its children.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.waiting import wait_until

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd + /proc process tree")

psutil = pytest.importorskip("psutil")


@pytest.fixture()
def wrapper_tree(tmp_path):
    """``wrapper`` (the unit's MainPID) -> [a ``gateway run`` child, a non-gateway card worker child]."""
    main_py = tmp_path / "hermes_cli" / "main.py"
    main_py.parent.mkdir()
    main_py.write_text("import time\ntime.sleep(60)\n")
    worker_py = tmp_path / "card_worker.py"
    worker_py.write_text("import time\ntime.sleep(60)\n")
    wrapper_py = tmp_path / "broker_wrapper.py"
    wrapper_py.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, {str(main_py)!r}, "gateway", "run"])
        subprocess.Popen([sys.executable, {str(worker_py)!r}])
        time.sleep(60)
    """))
    wrapper = subprocess.Popen([sys.executable, str(wrapper_py)])
    try:
        def started():
            children = psutil.Process(wrapper.pid).children()
            names = {Path(c.cmdline()[1]).name: c.pid for c in children if len(c.cmdline()) >= 2}
            return names if set(names) == {"main.py", "card_worker.py"} else None
        by_script = wait_until(started, timeout=10, what="the wrapper's two children")
        yield wrapper.pid, by_script["main.py"], by_script["card_worker.py"]
    finally:
        for child in psutil.Process(wrapper.pid).children(recursive=True):
            child.kill()
        wrapper.kill()
        wrapper.wait(timeout=10)


def _systemctl_for_main_pid(main_pid: int):
    def run(args, **kwargs):
        cmd = " ".join(str(a) for a in args)
        if args[:2] == ["systemctl", "--user"] and "list-units" in cmd:
            return MagicMock(returncode=0, stdout="hermes-gateway.service loaded active running Hermes\n", stderr="")
        if args[:2] == ["systemctl", "--user"] and "show" in cmd and "MainPID" in cmd:
            return MagicMock(returncode=0, stdout=f"{main_pid}\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return run


def _systemd_host(monkeypatch, main_pid: int):
    from hermes_cli import gateway as gateway_mod
    monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
    monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway_mod.subprocess, "run", _systemctl_for_main_pid(main_pid))
    return gateway_mod


def test_service_pids_include_the_gateway_child_of_a_wrapper_main_pid(wrapper_tree, monkeypatch):
    main_pid, gateway_pid, worker_pid = wrapper_tree
    gateway_mod = _systemd_host(monkeypatch, main_pid)

    pids = gateway_mod._get_service_pids(all_profiles=True)

    assert main_pid in pids
    assert gateway_pid in pids, "the unit's `gateway run` child must count as service-managed"
    assert worker_pid not in pids, "a non-gateway descendant (card worker) is never a gateway PID"


def test_update_manual_sweep_leaves_the_units_gateway_child_alone(monkeypatch, wrapper_tree, tmp_path):
    """The exact worker-2 path: the pidfile maps the unit's gateway child to profile ``default``.

    Before the fix the sweep armed a detached ``--replace`` relaunch for it and drained it."""
    main_pid, gateway_pid, _worker_pid = wrapper_tree
    gateway_mod = _systemd_host(monkeypatch, main_pid)
    from hermes_cli import update_cmd_fleet as fleet
    from hermes_cli import update_fleet_scope

    live = [gateway_pid]  # what the host-wide scan sees (the real one also reports the wrapper)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids",
                        lambda exclude_pids=None, all_profiles=False: [p for p in live if p not in (exclude_pids or set())])
    monkeypatch.setattr(gateway_mod, "find_profile_gateway_processes", lambda exclude_pids=None, strict=False: [
        gateway_mod.ProfileGatewayProcess(profile="default", path=tmp_path, pid=p)
        for p in live if p not in (exclude_pids or set())])
    armed, signalled, killed = [], [], []
    monkeypatch.setattr(gateway_mod, "_prepare_profile_gateway_update_restart",
                        lambda profile, pid: armed.append((profile, pid)) or "detached")
    monkeypatch.setattr(gateway_mod, "_wait_for_gateway_exit", lambda **kwargs: None)
    monkeypatch.setattr(update_fleet_scope, "drop_gateways_on_another_checkout", lambda pids, quiet=False: list(pids))
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, keep=(), quiet=False: list(pids))
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update",
                        lambda pid, budget, label, self_restart_pending=None: signalled.append(pid) or True)
    class _OsWithoutKill:  # only this module's os.kill: the fixture's teardown still needs the real one
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def kill(pid, sig):
            killed.append((pid, sig))

    monkeypatch.setattr(fleet, "os", _OsWithoutKill())
    out = fleet._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=["hermes-gateway"],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set())

    fleet._restart_manual_gateways(out, 45.0)

    assert armed == [], "no detached --replace relaunch may be armed for the unit's own gateway"
    assert signalled == [] and killed == []
    assert out.killed_pids == set() and out.relaunched_profiles == []
