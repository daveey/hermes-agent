"""Remote access is a declared contract, never inferred from arbitrary prose."""
import pytest

from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_coordination_placement as helpers

fleet = helpers.fleet


def card(body, requires=()):
    return {"title": "Check remote evidence", "body": body, "requires": list(requires)}


@pytest.mark.parametrize("body", [
    "on the hub, kill the stuck dispatcher pid",
    "delete the stale rows from kanban.db",
    "ssh hermes-hub and sudo reboot",
    "ssh worker-1",
    "read the log on the hub",
])
def test_undeclared_access_never_exempts_markers(fleet, body):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    assert kp.portable_reason(card(body), cfg) == "home gateway marker"


@pytest.mark.parametrize("requirement,label,body", [
    ("hub:read", "capability:hub-read", "read the log on the hub"),
    ("ssh:mbp", "capability:ssh-mbp", "ssh to the MacBook for a read"),
    ("ssh:mini", "capability:ssh-mini", "ssh mini diag"),
])
def test_declared_access_requires_every_destination(fleet, requirement, label, body):
    cfg, entry, machines = fleet
    args = card(body, [requirement])
    assert kp.portable_reason(args, cfg) is not None
    entry["labels"].append(label)
    assert kp.portable_reason(args, cfg) is None
    cfg["worker_pools"]["hub"].append("other")
    machines["other"] = dict(entry, labels=[])
    assert kp.portable_reason(args, cfg) is not None


def test_access_contract_is_rechecked_at_execution(fleet, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_placement as wp
    cfg, entry, _ = fleet
    args = card("read the log on the hub", ["hub:read"])
    entry["labels"] += helpers.ACCESS
    assert kp.portable_reason(args, cfg) is None
    monkeypatch.setattr(sync, "board_mode", lambda _: "primary")
    monkeypatch.setattr(sync, "sync_state", lambda _: {"gateway_id": "worker"})
    args.update(placement_policy="worker-first-v1", runs_on='["machine:worker"]')
    assert wp.execution_refusal(None, args) is None
    entry["labels"] = []
    assert wp.execution_refusal(None, args) is not None


@pytest.mark.parametrize("body", ["read ~/.hermes/state.db on the hub", "systemctl restart x on the hub"])
def test_independent_markers_survive_declared_access(fleet, body):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    assert kp.portable_reason(card(body, ["hub:read"]), cfg) is not None


def test_operator_extra_marker_survives_declared_access(fleet):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    cfg["extra_home_markers"] = [r"on the hub"]
    assert kp.portable_reason(card("read logs on the hub", ["hub:read"]), cfg) is not None


def test_legacy_routing_does_not_drop_access_contract(fleet):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    cfg["heavy_route"] = {"hub": "worker"}
    args = card("read logs on the hub", ["hub:read"])
    args["title"] = "Run pytest"
    assert kp._routeless_heavy(args, kp._Cfg(cfg), "hub", lambda _: True) is None


@pytest.mark.parametrize("policy,expected", [
    ("worker-first-v1", ["aws:example:read"]),
    (None, ["hub:read", "aws:example:read"]),
])
def test_preflight_keeps_enrolled_access_with_worker(monkeypatch, policy, expected):
    from types import SimpleNamespace
    from hermes_cli import kanban_capability_routing as routing
    from hermes_cli import kanban_db as kb
    task = SimpleNamespace(requires=["hub:read", "aws:example:read"],
                           placement_policy=policy, status="ready", body="read", title="read")
    monkeypatch.setattr(kb, "get_task", lambda *_: task)
    monkeypatch.setattr(routing, "_previous_routes", lambda *_: False)
    observed = []
    def route(conn, task_id, needs, **kwargs):
        observed.extend(needs)
        return {"test": True}
    monkeypatch.setattr(routing, "route", route)
    assert routing.preflight(None, "test") == {"test": True}
    assert observed == expected
