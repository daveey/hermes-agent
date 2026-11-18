"""Enrollment preflight is fresh evidence, not forwarding authority."""
import copy
import sqlite3

import pytest

from hermes_cli.kanban_origin_destination import require_destination
from tests.hermes_cli import test_origin_forward_decision as decisions
from tests.hermes_cli import test_origin_forward_adapter as adapter

forwarding = decisions.forwarding
authority = decisions.authority
world = adapter.world
envelope = adapter.envelope


def inventory(*names):
    return {"me": "macbook", "machines": [{"name": n, "state": "active"} for n in names]}


@pytest.mark.parametrize("value", [None, [], {}, {"me": "other", "machines": []},
    inventory("macbook", "hub") | {"me": "other"},
    {"me": "macbook", "machines": tuple(inventory("macbook", "hub")["machines"])},
    {"me": "macbook", "machines": {}}, inventory(), inventory("hub"),
    {"me": "macbook", "machines": [None]},
    {"me": "macbook", "machines": [{"name": 1, "state": "active"}]},
    inventory("bad/name", "macbook", "hub"), inventory("macbook", "hub", "hub"),
    {"me": "macbook", "machines": [{"name": "macbook", "state": "active"},
                                      {"name": "hub", "state": "revoked"}]}])
def test_malformed_registry_is_unavailable(value):
    with pytest.raises(ValueError, match="^origin_forward_inventory_unavailable$"):
        require_destination(value, gateway="macbook", destination="hub")


def test_valid_registry_distinguishes_unknown_destination():
    require_destination(inventory("macbook", "hub"), gateway="macbook", destination="hub")
    with pytest.raises(ValueError, match="^origin_forward_destination_unknown$"):
        require_destination(inventory("macbook"), gateway="macbook", destination="hub")


def test_preflight_is_after_authorization_before_freeze(forwarding):
    calls = []

    def lookup():
        calls.append(True)
        return inventory("macbook")

    with pytest.raises(ValueError, match="origin_forward_lease_invalid"):
        decisions.decide(forwarding, lease_lookup=lambda *args: None, inventory_lookup=lookup)
    assert calls == []
    with pytest.raises(ValueError, match="origin_forward_destination_unknown"):
        decisions.decide(forwarding, inventory_lookup=lookup)
    assert calls == [True]
    with sqlite3.connect(forwarding[1]["path"]) as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_new_admission_reads_fresh_inventory_but_replay_skips_it(forwarding):
    calls = []

    def lookup():
        calls.append(True)
        return inventory("macbook", "hub") if len(calls) == 1 else inventory("macbook")

    first = decisions.decide(forwarding, inventory_lookup=lookup)
    assert decisions.decide(forwarding, inventory_lookup=lookup) == first
    assert len(calls) == 1
    changed = copy.deepcopy(forwarding[0])
    changed["id"] += "-new"
    with pytest.raises(ValueError, match="origin_forward_destination_unknown"):
        decisions.decide(forwarding, changed, inventory_lookup=lookup)
    assert len(calls) == 2


def test_inventory_is_not_creator_equality_or_liveness_gate(forwarding):
    # A third enrolled destination need not be creator, origin, or recently seen.
    changed = copy.deepcopy(forwarding[0])
    changed["args"]["destination"]["gateway"] = "third"
    data = inventory("macbook", "third")
    data["machines"][1].update(last_seen=0, pickup=None, hold=True)
    assert decisions.decide(forwarding, changed, inventory_lookup=lambda: data)["to_gw"] == "third"


def test_lease_expiry_during_inventory_read_is_rechecked(forwarding):
    clock = [1000]

    def lookup():
        clock[0] = 2000
        return inventory("macbook", "hub")

    with pytest.raises(ValueError, match="origin_forward_lease_invalid"):
        decisions.decide(forwarding, inventory_lookup=lookup, now_ms=lambda: clock[0])


@pytest.mark.parametrize("mode,status,code", [
    ("unknown", "rejected", "origin_forward_destination_unknown"),
    ("malformed", "pending", "origin_forward_inventory_unavailable"),
    ("offline", "pending", "origin_forward_evidence_unavailable")])
def test_real_adapter_inventory_failure_and_recovery(world, monkeypatch, capsys, envelope, mode, status, code):
    from hermes_cli.kanban_sync_client import Hub

    def unavailable(self):
        if mode == "offline":
            raise OSError("offline")
        if mode == "malformed":
            return {}
        return {"me": "origin", "machines": [{"name": "origin", "state": "active"}]}

    monkeypatch.setattr(Hub, "machines", unavailable)
    result = adapter.invoke(world, monkeypatch, capsys, envelope)
    assert result == {"status": status, "code": code, "handoff_complete": False}
    monkeypatch.setattr(Hub, "machines", lambda self: {"me": "origin", "machines": [
        {"name": "origin", "state": "active"}, {"name": "owner", "state": "active"}]})
    assert adapter.invoke(world, monkeypatch, capsys, envelope)["status"] == "prepared"
