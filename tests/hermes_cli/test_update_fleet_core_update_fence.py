"""[daveey] A fenced core update must not report a false ``gateway_restart`` follow-up.

hermes-sync runs ``hermes update --yes`` inside its core-update fence: ``<home>/core-update/intent``
holds the fence owner's pid and every ``hermes-gateway-launcher`` parks while it exists. The
launcher-supervised successor therefore starts only AFTER the update exits (David-MBP 2026-10-28:
update exit 00:55:50, gateway up 00:55:57). The post-update probe polled the full settle window,
found no rows and printed "Fleet version check returned no rows" plus an owed ``gateway_restart``
on every fenced update. Controls: no fence, and a fence held by a process that is not this
update's ancestor, keep the fail-closed warning.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from hermes_cli.update_inventory import RuntimeRecord


def _plan():
    return types.SimpleNamespace(runtimes=[RuntimeRecord(kind="gateway", profile="default")])


def _write_intent(home, pid):
    (home / "core-update").mkdir(parents=True, exist_ok=True)
    (home / "core-update" / "intent").write_text(json.dumps({"pid": pid, "created_at": 0}) + "\n")


@pytest.fixture
def harness(monkeypatch, tmp_path):
    from hermes_cli import main, update_cmd, update_cmd_fleet as fleet, update_cmd_fleet_verify as fleet_verify
    from hermes_cli import update_gateway_fence, update_receipt

    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(fleet_verify, "_print_legacy_units_warning", lambda: None)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._refresh_dashboard_after_update", lambda **kw: None)
    monkeypatch.setattr(update_cmd, "_surviving_pre_update_serve_runtimes", lambda plan: [])
    monkeypatch.setattr("hermes_cli.update_inventory.report_unaccounted_runtimes", lambda rows: False)
    migrated = []
    monkeypatch.setattr("hermes_cli.gateway_migrate.maybe_auto_migrate_after_update", lambda: migrated.append(1))
    cleared = []
    monkeypatch.setattr(fleet, "_clear_fleet_restart_pending_marker", lambda: cleared.append(True))
    monkeypatch.setattr(fleet_verify, "_record_owed_gateway_inventory", lambda plan, stopped=(): None)

    # Simulated clock. The launcher-supervised successor cannot appear while this process runs
    # (the launcher parks on the fence owner's intent), so every probe made here sees no row; the
    # post-release "successor appears after N seconds" half is hermes-sync's (tests/test_core_fence_verify.py).
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    def collect(**kwargs):
        return []

    monkeypatch.setattr(fleet_verify, "_time", types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep))
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", collect)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr(update_gateway_fence, "_gateway_homes", lambda: [home])

    def run():
        restart = fleet._GatewayRestartOutcome(
            incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=[],
            failed_or_stale_units=[], relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
        )
        update_receipt.begin_update_receipt()
        fleet_verify._verify_fleet_after_update(restart, _pre_update_plan=_plan(),
                                                _windows_gateway_resume=None, update_complete=True)
        receipt = json.loads((update_cmd.get_hermes_home() / "logs/update_receipts/latest.json").read_text())
        return restart, receipt

    return types.SimpleNamespace(home=home, run=run, clock=clock, cleared=cleared, migrated=migrated,
                                 fence=update_gateway_fence)


def test_fenced_update_defers_verification_instead_of_owing_a_restart(harness, monkeypatch, capsys):
    # The fence owner is this update's ancestor (hermes-sync -> hermes update).
    _write_intent(harness.home, os.getppid())
    restart, receipt = harness.run()
    out = capsys.readouterr().out
    assert "returned no rows" not in out
    assert "did not finish" not in out
    assert "held by the core-update fence" in out
    assert restart.incomplete is False
    assert [f["step"] for f in receipt.get("followups", [])] == []
    # Unverified: the armed obligation is kept (not cleared) and the topology is not migrated.
    assert harness.cleared == []
    assert harness.migrated == []
    # No 120 s settle wait inside the fence: one probe, no simulated time spent.
    assert harness.clock["now"] == 0.0


def test_without_a_fence_an_absent_successor_still_owes_the_restart(harness, capsys):
    restart, receipt = harness.run()
    out = capsys.readouterr().out
    assert "returned no rows" in out
    assert restart.incomplete is True
    assert [f["step"] for f in receipt["followups"]] == ["gateway_restart"]
    assert harness.cleared == []


def test_an_intent_from_a_non_ancestor_is_not_a_fence(harness, capsys):
    # A live process that is not this update's ancestor (pid 1 is never accepted; use our own pid,
    # which is not in our ancestor set) proves nothing about this update's gateways.
    _write_intent(harness.home, os.getpid())
    restart, receipt = harness.run()
    assert "returned no rows" in capsys.readouterr().out
    assert restart.incomplete is True
    assert [f["step"] for f in receipt["followups"]] == ["gateway_restart"]


def test_fence_still_flags_a_stale_gateway_it_can_see(harness, monkeypatch, capsys):
    # The fence only explains ABSENT rows; a visible stale gateway still owes its restart.
    from hermes_cli import update_receipt
    _write_intent(harness.home, os.getppid())
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **kw: [
        {"profile": "default", "pid": 777, "state": "stale", "code_sha": "b" * 40}])
    monkeypatch.setattr("hermes_cli.update_cmd_stale_survivors.signal_stale_fleet_survivors", lambda *a, **k: None)
    restart, receipt = harness.run()
    assert restart.incomplete is True
    assert [f["step"] for f in receipt["followups"]] == ["gateway_restart"]


def test_ancestor_fence_owner_reads_only_live_ancestor_intents(tmp_path):
    from hermes_cli.update_gateway_fence import ancestor_fence_owner
    home = tmp_path / "h"
    home.mkdir()
    assert ancestor_fence_owner([home]) is None
    _write_intent(home, os.getppid())
    assert ancestor_fence_owner([home]) == os.getppid()
    (home / "core-update" / "intent").write_text("not json")
    assert ancestor_fence_owner([home]) is None
