"""The host restart obligation settles once every checkout-backed gateway runs the checkout.

hermes-hub, 2026-11-03: an update restarted `default` onto the pulled code and handed the Ask Eve
profiles (whose gateways run the askeve-runtime's own pinned core) back to their units, but it also
SIGTERMed a stray ``gateway run`` process that held no profile's ``gateway.pid``. That stop was
recorded as ``{"profile": None, "stopped_unmapped": True}`` with no home, and the inventory named the
Ask Eve profiles too. Every later CLI start then printed "did not restart running gateways"
although nothing checkout-backed was stale: an unmapped row with no identity can only be settled by
a further local gateway that no owed profile explains, and that never comes.

Each owed row class gets its own case: the external-checkout runtime, the unmapped stop with a
readable home, and the controls that must keep warning (a stale or down checkout gateway, an
unmapped stop whose home has no live gateway, an unmapped stop whose home was unreadable).
"""

from __future__ import annotations

import json
import types

import pytest

HEAD = "ceff8ab7358f07324e7fcd09526ccb197aa518d4"
ASKEVE = "14a1244773e6304204e451b30c829cb77aa9edbd"


def _plan(runtimes):
    return types.SimpleNamespace(runtimes=runtimes)


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Isolated obligation store and checkout HEAD; ``rows`` is the live fleet the probe sees."""
    from hermes_cli import update_cmd, update_cmd_fleet as fleet, update_cmd_fleet_verify as fleet_verify
    from hermes_cli import update_host_obligation as host_obligation, update_receipt

    monkeypatch.setattr(host_obligation, "host_obligation_path", lambda: tmp_path / "host-update-restart.json")
    monkeypatch.setattr(fleet, "_current_checkout_sha", lambda: HEAD)
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: HEAD)
    monkeypatch.setattr(fleet_verify, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._refresh_dashboard_after_update", lambda **kw: None)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda plan: [])
    monkeypatch.setattr(fleet_verify, "_FLEET_PROBE_SETTLE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr("hermes_cli.update_cmd_stale_survivors.signal_stale_fleet_survivors", lambda *a: None)
    rows: list = []
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **kw: [dict(r) for r in rows])
    home = update_cmd.get_hermes_home()
    (home / "profiles" / "askeve-builder").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "work").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(rows=rows, home=home, fleet=fleet, verify=fleet_verify, receipt=update_receipt)


def _current(profile, pid):
    return {"profile": profile, "pid": pid, "state": "current", "code_sha": HEAD,
            "code_root": "/srv/hermes-agent"}


def _external(profile, pid):
    return {"profile": profile, "pid": pid, "state": "external", "code_sha": ASKEVE,
            "code_root": "/srv/askeve-runtime/core-14a1244773e6"}


def _run_update(h, *, stopped_unmapped=(), homes=None, phase_errors=()):
    """The real post-restart verification, ending in an owed restart (the obligation is armed)."""
    from hermes_cli.update_inventory import RuntimeRecord

    h.fleet._write_fleet_restart_pending_marker(expected_sha=HEAD)  # armed by the pull
    out = h.fleet._GatewayRestartOutcome(
        incomplete=bool(phase_errors), phase_errors=list(phase_errors), pre_restart_gateway_pids=[1532017],
        restarted_services=["hermes-gateway", "hermes-gateway-askeve-builder"], failed_or_stale_units=[],
        relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(stopped_unmapped),
        stopped_unmapped_pids=set(stopped_unmapped),
    )
    # Set as an attribute, not a constructor field, so the case runs (and fails on the gap it
    # tests) against an outcome type that predates the field.
    out.stopped_unmapped_homes = dict(homes or {})
    plan = _plan([
        RuntimeRecord(kind="gateway", profile="default", pid=1532017, code_sha="9ba5754ca9e9"),
        RuntimeRecord(kind="gateway", profile="askeve-builder", pid=1533512, code_sha=ASKEVE),
    ])
    h.receipt.begin_update_receipt()
    h.verify._verify_fleet_after_update(out, _pre_update_plan=plan, _windows_gateway_resume=None,
                                        update_complete=True)
    assert h.fleet._fleet_restart_obligation_armed(), "the run must leave an owed restart for this case"
    return json.loads(h.fleet._obligation_fields()["inventory"])["runtimes"]


# --- external-checkout runtime (askeve-runtime's own pinned core) -------------------------------

def test_an_external_checkout_gateway_is_not_owed_a_restart_onto_this_checkout(host):
    host.rows[:] = [_current("default", 2542475), _external("askeve-builder", 2544484)]
    inventory = _run_update(host, phase_errors=["boom"])

    assert [r["profile"] for r in inventory] == ["default"], "a runtime of another checkout was recorded as owed"
    # The Ask Eve gateway restarting (no row for a moment) or down for its own reasons is that
    # runtime's business: the checkout-backed fleet is current, so nothing of this update is owed.
    host.rows[:] = [_current("default", 2542475)]
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is False
    assert not host.fleet._fleet_restart_obligation_armed()


def test_a_stale_checkout_gateway_still_owes_the_restart_next_to_an_external_one(host):
    host.rows[:] = [_current("default", 2542475), _external("askeve-builder", 2544484)]
    _run_update(host, phase_errors=["boom"])

    host.rows[:] = [dict(_current("default", 1532017), state="stale", code_sha="9ba5754ca9e9"),
                    _external("askeve-builder", 2544484)]
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is True
    host.rows[:] = [_external("askeve-builder", 2544484)]  # `default` down
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is True
    assert host.fleet._fleet_restart_obligation_armed()


# --- unmapped stop (no profile, no code) ---------------------------------------------------------

def test_an_unmapped_stop_on_a_home_whose_gateway_runs_the_checkout_is_settled(host):
    host.rows[:] = [_current("default", 2542475), _external("askeve-builder", 2544484)]
    inventory = _run_update(host, stopped_unmapped=[2549548], homes={2549548: str(host.home)})

    assert {"kind": "gateway", "profile": None, "pid": 2549548, "stopped_unmapped": True,
            "home": str(host.home)} in inventory
    # The home's one lock-holding gateway is current on HEAD: the stray process owes nothing more.
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is False
    assert not host.fleet._fleet_restart_obligation_armed()


def test_an_unmapped_stop_on_a_home_with_no_live_gateway_keeps_warning(host):
    work = host.home / "profiles" / "work"
    host.rows[:] = [_current("default", 2542475), _external("askeve-builder", 2544484)]
    _run_update(host, stopped_unmapped=[2549548], homes={2549548: str(work)})

    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is True
    host.rows.append(_current("work", 2600000))  # `hermes -p work gateway run`
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is False


def test_an_unmapped_stop_with_an_unreadable_home_keeps_the_old_rule(host):
    host.rows[:] = [_current("default", 2542475), _external("askeve-builder", 2544484)]
    inventory = _run_update(host, stopped_unmapped=[2549548])

    assert {"kind": "gateway", "profile": None, "pid": 2549548, "stopped_unmapped": True} in inventory
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is True


def test_the_manual_sweep_reads_each_unmapped_home_before_the_stop(monkeypatch):
    from hermes_cli import gateway, update_cmd_fleet as fleet, update_cmd_posix_pause, update_fleet_scope

    alive = {2549548}
    monkeypatch.setattr(gateway, "_get_service_pids", lambda all_profiles=False: set())
    monkeypatch.setattr(update_cmd_posix_pause, "already_restarted", lambda: {"pids": set()})
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda exclude_pids=None, all_profiles=False: [2549548])
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda exclude_pids=None, strict=False: [])
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, keep=(), quiet=False: list(pids))
    monkeypatch.setattr(update_fleet_scope, "gateway_pid_home",
                        lambda pid: "/srv/home" if pid in alive else None)
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: alive.discard(pid))
    out = fleet._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
    )
    fleet._restart_manual_gateways(out, 0)

    assert out.stopped_unmapped_pids == {2549548}
    assert out.stopped_unmapped_homes == {2549548: "/srv/home"}


def test_the_stale_survivor_sweep_reads_each_manual_home_before_the_signal(monkeypatch):
    from hermes_cli import gateway, gateway_supervised_restart, update_cmd_fleet as fleet, update_fleet_scope
    from hermes_cli.update_cmd_stale_survivors import signal_stale_fleet_survivors

    alive = {4242}
    monkeypatch.setattr(gateway, "_get_service_pids", lambda all_profiles=False: set())
    monkeypatch.setattr(gateway_supervised_restart, "gateway_declares_external_supervisor", lambda pid, home=None: False)
    monkeypatch.setattr(update_fleet_scope, "gateway_pid_home", lambda pid: "/srv/work" if pid in alive else None)
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update",
                        lambda pid, budget, label, **kw: alive.discard(pid) or True)
    out = fleet._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
    )
    signal_stale_fleet_survivors([{"profile": None, "pid": 4242, "state": "stale"}], out, 0)

    assert out.stopped_unmapped_pids == {4242}
    assert out.stopped_unmapped_homes == {4242: "/srv/work"}


def test_an_unmapped_stop_is_owed_to_its_own_homes_profile_not_to_any_live_gateway(host):
    # A named home maps to ITS profile: `default` and an unrelated `ops` coming back current are no
    # successor of a stop on `work` (the home-less rule accepts `ops`, which no owed row explains).
    work = host.home / "profiles" / "work"
    (host.home / "profiles" / "ops").mkdir(parents=True, exist_ok=True)
    host.rows[:] = [_current("default", 2542475), _external("askeve-builder", 2544484), _current("ops", 2544999)]
    _run_update(host, stopped_unmapped=[2549548], homes={2549548: str(work)})

    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is True
    host.rows.append(_current("work", 2600000))
    assert host.fleet._update_owes_fleet_restart(receipt={}, pending_manual=[]) is False


def test_the_stale_survivor_sweep_records_the_home_of_the_rows_own_profile(monkeypatch):
    from hermes_cli import gateway, gateway_supervised_restart, update_cmd, update_cmd_fleet as fleet
    from hermes_cli import update_fleet_scope
    from hermes_cli.update_cmd_stale_survivors import signal_stale_fleet_survivors

    work = update_cmd.get_hermes_home() / "profiles" / "work"
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gateway, "_get_service_pids", lambda all_profiles=False: set())
    monkeypatch.setattr(gateway_supervised_restart, "gateway_declares_external_supervisor", lambda pid, home=None: False)
    monkeypatch.setattr(update_fleet_scope, "gateway_pid_home", lambda pid: "/guessed/from/environ")
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda pid, budget, label, **kw: True)
    out = fleet._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=[],
        failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
    )
    signal_stale_fleet_survivors([{"profile": "work", "pid": 4343, "state": "stale"}], out, 0)

    assert out.stopped_unmapped_homes == {4343: str(work)}
