"""A gateway pinned to its own core never blocks, and is never bounced by, ``hermes update``.

hermes-hub, 2026-11-11: ``hermes-gateway-askeve-builder`` runs through a drop-in whose ``ExecStart``
and ``PYTHONPATH`` name ``~/.hermes/askeve-runtime/core-<sha>``, a core older than the idle pause.
It answered ``unsupported`` to the pre-swap pause, and eleven hub updates in a row deferred on it
("restart that gateway on the installed code when idle" cannot work: a restart comes back on the
pin). Moving the checkout cannot reach that gateway's code, so the update leaves it alone.

The gateway here is a real process started the way the drop-in starts it; systemd is the seam.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import control_socket
from hermes_cli import dashboard_procs
from hermes_cli import update_cmd_fleet as fleet
from hermes_cli import update_cmd_posix_pause as pause

# The pinned gateway is a stub ``python -m hermes_cli.main gateway run`` whose ``hermes_cli`` is the
# fixture's own package; each test reaps it.
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.spawns_gateway_lookalike]

UNIT = "hermes-gateway-askeve-builder"
SCOPE_CMD = ["systemctl", "--user"]


def _spawn(argv, env, cwd):
    proc = subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    assert proc.stdout is not None and proc.stdout.readline().strip() == b"up", \
        "premise: the fixture gateway never started"
    return proc


@pytest.fixture
def host(monkeypatch, tmp_path):
    """An install root with an ``askeve-builder`` profile, its pinned drop-in, and a fake systemd
    that lists the unit on whichever PID the test puts in ``state['pid']``."""
    root = tmp_path / "root" / ".hermes"
    home = root / "profiles" / "askeve-builder"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_cli.update_receipt._profile_homes",
                        lambda: [("default", root), ("askeve-builder", home)])
    core = tmp_path / "askeve-runtime" / "core-d426474bc63b"
    (core / "hermes_cli").mkdir(parents=True)
    (core / "hermes_cli" / "__init__.py").write_text("")
    (core / "hermes_cli" / "main.py").write_text("import time\nprint('up', flush=True)\ntime.sleep(300)\n")
    dropin = tmp_path / "systemd" / f"{UNIT}.service.d" / "50-askeve-pinned-owner.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text(f"[Service]\nExecStart=\nExecStart={sys.executable} -m hermes_cli.main gateway run --quiet\n"
                      f"Environment=PYTHONPATH={core}\n")
    state = {"pid": 0, "writes": [], "repairs": [], "asked": [], "procs": []}

    def fake_systemctl(cmd, *, timeout):
        def done(stdout=""):
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if "list-units" in cmd:
            return done(f"{UNIT}.service loaded active running Hermes\n" if "--user" in cmd else "")
        if "is-active" in cmd:
            return done("active\n")
        if "show" in cmd and "--property=MainPID" in cmd:
            return done(f"{state['pid']}\n")
        if "show" in cmd and "--property=Environment" in cmd:
            return done(f"HERMES_HOME={home}\n")
        if "show" in cmd and "--property=ControlGroup" in cmd:
            return done(f"/user.slice/fixture/{UNIT}.service\n")
        if "show" in cmd:
            return done()
        state["writes"].append(cmd)  # stop / start / restart / reset-failed
        return done()

    monkeypatch.setattr(fleet, "_systemctl", fake_systemctl)
    monkeypatch.setattr(fleet, "_repair_unit_without_fatal_exit_park",
                        lambda *a, **k: state["repairs"].append(a))
    monkeypatch.setattr("hermes_cli.gateway.supports_systemd_services", lambda: True)
    monkeypatch.setattr("hermes_cli.gateway._ensure_user_systemd_env", lambda: None)
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(home))
    # An old core's control server: it does not know the idle pause.
    monkeypatch.setattr(control_socket, "pause_gateway_for_update",
                        lambda h, **k: state["asked"].append(str(h)) or {"unsupported": True})
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_discover_bare", lambda service_pids: ([], []))
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    state.update(root=root, home=home, core=core, dropin=dropin, dropin_before=dropin.read_bytes())
    yield state
    for proc in state["procs"]:
        proc.kill()
        proc.wait(timeout=10)


def _run_pinned(host, *, status_record: bool) -> int:
    """Start the gateway exactly as the drop-in does: a python outside the pinned core, the core on
    PYTHONPATH. ``status_record`` adds the ``gateway_state.json`` a real gateway publishes."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    env.update(PYTHONPATH=f"{host['core']}{os.pathsep}{host['core'].parent / 'app'}", PYTHONSAFEPATH="1")
    proc = _spawn([sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--quiet"], env, host["home"])
    host["procs"].append(proc)
    host["pid"] = proc.pid
    if status_record:
        (host["home"] / "gateway_state.json").write_text(json.dumps({
            "pid": proc.pid, "argv": [str(host["core"] / "hermes_cli" / "main.py"), "gateway", "run", "--quiet"]}))
    return proc.pid


def _run_on_checkout(host) -> int:
    """A gateway of the same unit whose code root is not provably elsewhere (control)."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    proc = _spawn([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(300)"], env, host["home"])
    host["procs"].append(proc)
    host["pid"] = proc.pid
    return proc.pid


@pytest.mark.parametrize("status_record", [True, False], ids=["status-record", "pythonpath-only"])
def test_update_proceeds_past_a_pinned_unit_that_cannot_idle_pause(host, capsys, status_record):
    """The pinned unit answers ``unsupported``; the pause must not ask it, stop it or defer on it."""
    _run_pinned(host, status_record=status_record)

    verdict = pause._pause(pause._empty_token())

    out = capsys.readouterr().out
    assert verdict is None, f"the update was deferred on a gateway it cannot affect: {verdict}"
    assert host["asked"] == [], "the pinned gateway was asked for an idle pause it cannot answer"
    assert host["writes"] == [] and host["repairs"] == [], "the pinned unit was touched through systemd"
    assert UNIT in out and "running its own checkout" in out and str(host["core"]) in out, out
    assert host["procs"][0].poll() is None, "the pinned gateway was stopped"
    assert host["dropin"].read_bytes() == host["dropin_before"], "the pin (drop-in ExecStart) changed"


def test_a_checkout_unit_that_cannot_idle_pause_still_defers(host):
    """Control: the same unit NOT proven on another checkout keeps the refusal (its code is the one
    being replaced)."""
    _run_on_checkout(host)

    verdict = pause._pause(pause._empty_token())

    assert verdict and f"{UNIT} does not support idle pause" in verdict
    assert host["asked"] == [str(host["home"])]


def test_post_commit_restart_leaves_a_pinned_unit_on_its_pin(host, monkeypatch):
    """The fleet restart must not drain or restart the pinned unit (it would only come back on its
    pin); the same unit running this checkout is still restarted (control)."""
    drained: list[int] = []
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda pid, *a, **k: drained.append(pid) or True)
    monkeypatch.setattr(fleet, "_wait_for_service_active", lambda *a, **k: True)

    def restart():
        restarted, failed = [], []
        fleet._restart_one_systemd_gateway_unit(
            UNIT, scope="user", scope_cmd=SCOPE_CMD, drain_budget=5.0, _manage_cmd_cache={},
            restarted_services=restarted, failed_or_stale_units=failed)
        return restarted, failed

    _run_pinned(host, status_record=True)
    assert restart() == ([], [])
    assert drained == [] and host["writes"] == [] and host["repairs"] == []
    assert host["dropin"].read_bytes() == host["dropin_before"]

    control = _run_on_checkout(host)
    (host["home"] / "gateway_state.json").unlink()
    restarted, failed = restart()
    assert drained == [control] and restarted == [UNIT] and failed == []


def test_best_effort_restart_skips_a_pinned_unit(host):
    """The blunt post-commit restart (no drain) applies the same ownership rule."""
    _run_pinned(host, status_record=True)
    failed: list[str] = []
    fleet._restart_systemd_gateway_units_best_effort(failed, fleet._systemd_gateway_unit_listings())
    assert failed == [] and host["writes"] == []
