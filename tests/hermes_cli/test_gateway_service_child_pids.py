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


def _child_scripts(children):
    names = {}
    for child in children:
        try:
            argv = child.cmdline()
        except psutil.NoSuchProcess:
            continue
        if len(argv) >= 2:
            names[Path(argv[1]).name] = child.pid
    return names


def test_child_scripts_reads_each_command_line_once():
    child = MagicMock(pid=123)
    child.cmdline.side_effect = [["python", "/fixture/main.py", "gateway", "run"], []]
    assert _child_scripts([child]) == {"main.py": 123}
    child.cmdline.assert_called_once_with()


@pytest.mark.parametrize("argv", [[], ["python"]])
def test_child_scripts_waits_for_complete_argv(argv):
    child = MagicMock(pid=123)
    child.cmdline.return_value = argv
    assert _child_scripts([child]) == {}
    child.cmdline.assert_called_once_with()


def test_child_scripts_tolerates_exit_but_not_access_denied():
    child = MagicMock(pid=123)
    child.cmdline.side_effect = psutil.NoSuchProcess(123)
    assert _child_scripts([child]) == {}
    child.cmdline.side_effect = psutil.AccessDenied(123)
    with pytest.raises(psutil.AccessDenied):
        _child_scripts([child])


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
            names = _child_scripts(children)
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


# --- the walk stops at the unit's gateway -------------------------------------------------------------
# Kanban workers and agent terminals run below the unit's gateway. A `gateway run` an agent starts there is
# a MANUAL gateway: the update sweep must see it, and `_stop_host_multiplexer`/uninstall must not take it
# for the unit's own. Each tree below is real processes; every node records its pid in ``pids/<role>``.

_NODE_SRC = textwrap.dedent("""
    import json, os, subprocess, sys, time
    spec = json.load(open(os.environ["TREE_SPEC"]))
    role = os.environ["TREE_ROLE"]
    for child in spec[role]["children"]:
        subprocess.Popen([sys.executable, *spec[child]["argv"]], env={**os.environ, "TREE_ROLE": child})
    with open(os.path.join(os.environ["TREE_PIDS"], role + ".tmp"), "w") as f:
        f.write(str(os.getpid()))
    os.replace(os.path.join(os.environ["TREE_PIDS"], role + ".tmp"), os.path.join(os.environ["TREE_PIDS"], role))
    time.sleep(60)
""")


@pytest.fixture()
def process_tree(tmp_path):
    """Start ``{role: (kind, [child roles])}`` from role ``root``; kind is ``gateway`` (a ``hermes_cli/main.py
    gateway run`` command line), ``wrapper_matching`` (gateway argv carried after ``--`` as data, like
    ``github_quota.py … -- hermes gateway run``) or ``plain``. Returns ``{role: pid}``."""
    import json

    gateway_py = tmp_path / "hermes_cli" / "main.py"
    gateway_py.parent.mkdir()
    plain_py = tmp_path / "card_worker.py"
    for script in (gateway_py, plain_py):
        script.write_text(_NODE_SRC)
    procs = []

    def start(tree: dict):
        argv_for = {
            "gateway": [str(gateway_py), "gateway", "run"],
            "wrapper_matching": [str(plain_py), "--reason=unit", "--", str(gateway_py), "gateway", "run"],
            "plain": [str(plain_py)],
        }
        spec = {role: {"argv": argv_for[kind], "children": children} for role, (kind, children) in tree.items()}
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))
        pids_dir = tmp_path / "pids"
        pids_dir.mkdir()
        env = {**os.environ, "TREE_SPEC": str(spec_path), "TREE_PIDS": str(pids_dir), "TREE_ROLE": "root"}
        procs.append(subprocess.Popen([sys.executable, *spec["root"]["argv"]], env=env))

        def all_started():
            if not all((pids_dir / role).exists() for role in tree):
                return None
            return {role: int((pids_dir / role).read_text()) for role in tree}
        return wait_until(all_started, timeout=20, what="every node of the process tree")

    try:
        yield start
    finally:
        for proc in procs:
            try:
                descendants = psutil.Process(proc.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                descendants = []
            for child in reversed(descendants):  # leaves first: an orphan leaves the test's subtree
                child.kill()
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.spawns_gateway_lookalike
def test_walk_stops_at_the_wrapper_child_gateway(process_tree, monkeypatch):
    """wrapper -> gateway -> card worker -> nested manual `gateway run`: only the first gateway is the unit's."""
    pids = process_tree({
        "root": ("plain", ["gateway"]),
        "gateway": ("gateway", ["worker"]),
        "worker": ("plain", ["nested"]),
        "nested": ("gateway", []),
    })
    from hermes_cli.gateway_service_members import gateway_descendant_pids

    assert gateway_descendant_pids(pids["root"]) == {pids["gateway"]}
    gateway_mod = _systemd_host(monkeypatch, pids["root"])
    service = gateway_mod._get_service_pids(all_profiles=True)
    assert pids["nested"] not in service, "an agent's `gateway run` below the unit gateway is manual"
    assert service == {pids["root"], pids["gateway"]}


@pytest.mark.spawns_gateway_lookalike
def test_no_descent_when_main_pid_is_the_gateway(process_tree, monkeypatch):
    pids = process_tree({
        "root": ("gateway", ["worker"]),
        "worker": ("plain", ["nested"]),
        "nested": ("gateway", []),
    })
    from hermes_cli.gateway_service_members import gateway_descendant_pids

    assert gateway_descendant_pids(pids["root"]) == set()
    gateway_mod = _systemd_host(monkeypatch, pids["root"])
    assert gateway_mod._get_service_pids(all_profiles=True) == {pids["root"]}


@pytest.mark.spawns_gateway_lookalike
def test_a_wrapper_whose_command_line_matches_is_walked_through(process_tree):
    """The worker-node wrapper (``github_quota.py … -- hermes gateway run``) matches the gateway matcher itself,
    so "stop at the first match" alone would stop at the wrapper and lose the unit's real gateway (PR #1523)."""
    pids = process_tree({
        "root": ("wrapper_matching", ["gateway"]),
        "gateway": ("gateway", ["worker"]),
        "worker": ("plain", ["nested"]),
        "nested": ("gateway", []),
    })
    from gateway.status import _read_process_cmdline, looks_like_gateway_command_line
    from hermes_cli.gateway_service_members import gateway_descendant_pids

    assert looks_like_gateway_command_line(_read_process_cmdline(pids["root"])), "fixture: wrapper must match"
    assert gateway_descendant_pids(pids["root"]) == {pids["gateway"]}
