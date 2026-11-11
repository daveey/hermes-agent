"""Coordination tooling is portable only to attested worker destinations."""
import time

import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn


@pytest.fixture
def fleet(monkeypatch):
    cfg = {"worker_pools": {"hub": ["worker"]}, "assignees": ["default"]}
    entry = {"state": "active", "pickup": "explicit", "last_seen": time.time() * 1000,
             "labels": ["capability:coordination-read", "capability:token-broker",
                        "capability:core-ship", "capability:tailnet-exec"]}
    machines = {"worker": entry}
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(wn, "registry", lambda: {"machines": machines})
    return cfg, entry, machines


def reason(body, cfg):
    return kp.portable_reason({"title": "Read coordination evidence", "body": body}, cfg)


def test_attested_reader_and_broker_are_portable(fleet):
    cfg, _, _ = fleet
    assert reason("cross_task read; hermes-hub status; token-broker exec for gh read", cfg) is None


def test_missing_reader_is_named_before_enrollment(fleet):
    cfg, entry, _ = fleet
    entry["labels"] = []
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("field,value", [
    ("state", "disabled"), ("pickup", None), ("hold", True),
    ("last_seen", 0), ("last_seen", float("inf")), ("last_seen", "invalid"),
    ("last_seen", (time.time() + 3600) * 1000), ("labels", "capability:coordination-read"),
])
def test_unusable_attestation_fails_closed(fleet, field, value):
    cfg, entry, _ = fleet
    entry[field] = value
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


def test_every_member_must_carry_the_capability(fleet):
    cfg, _, machines = fleet
    cfg["worker_pools"]["hub"].append("other")
    machines["other"] = {"labels": []}
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("body", [
    "hermes-hub status; hermes-hub create-task other title", "hermes-hub sync",
    "cross_task comment", "cross_task", "hermes-hub unknown-command",
])
def test_write_or_ambiguous_actions_stay_home(fleet, body):
    cfg, _, _ = fleet
    assert reason(body, cfg) is not None


@pytest.mark.parametrize("body", [
    "hermes-hub status; systemctl restart service", "hermes-hub status; ssh server",
    "hermes-hub status; ~/code/_wt/existing", "hermes-hub status; ~/.hermes/state.db",
])
def test_independent_host_markers_remain(fleet, body):
    cfg, _, _ = fleet
    assert reason(body, cfg) is not None


def test_extra_marker_cannot_be_exempted(fleet):
    cfg, _, _ = fleet
    cfg["extra_home_markers"] = [r"hermes-hub"]
    assert reason("hermes-hub status", cfg) == "home gateway marker"


def test_no_pool_cannot_borrow_another_origins_attestation(fleet):
    cfg, _, _ = fleet
    cfg["worker_pools"] = {"another": ["worker"]}
    assert reason("hermes-hub status", cfg) is not None


def test_actual_claim_rechecks_changed_labels(fleet, monkeypatch):
    from hermes_cli import kanban_runs_on_capabilities as rc
    cfg, entry, _ = fleet
    card = {"title": "cross_task read", "placement_policy": "worker-first-v1"}
    assert reason(card["title"], cfg) is None
    assert rc.claim_refusal(card, me="worker") is None
    entry["labels"] = []
    assert rc.claim_refusal(card, me="worker") == "needs worker coordination capability"
    card["placement_policy"] = None
    assert rc.claim_refusal(card, me="worker") is None


def test_legacy_target_cannot_borrow_pool_labels(fleet):
    cfg, _, _ = fleet
    args = {"title": "Run pytest", "body": "hermes-hub status"}
    assert kp.portable_reason(args, cfg) is None
    cfg["heavy_route"] = {"hub": "worker"}
    assert kp._routeless_heavy(args, kp._Cfg(cfg), "hub", lambda _: True) is None
    # Legacy pins cannot retain a requirement; even the capable target stays excluded.
    cfg["heavy_route"]["hub"] = "other"
    assert kp._routeless_heavy(args, kp._Cfg(cfg), "hub", lambda _: True) is None


@pytest.mark.parametrize("pool", [None, [], "worker", [1]])
def test_malformed_pool_is_not_attested(fleet, pool):
    cfg, _, _ = fleet
    cfg["worker_pools"]["hub"] = pool
    assert reason("cross_task read", cfg) is not None


def test_missing_registry_fails_closed(fleet, monkeypatch):
    cfg, _, _ = fleet
    monkeypatch.setattr(wn, "registry", lambda: None)
    assert reason("cross_task read", cfg) is not None


@pytest.mark.parametrize("skill", ["token-broker", "hermes-mods-repo-core-dev", "patching-local-hermes-workflow"])
def test_skill_pin_requires_attestation(fleet, skill):
    cfg, entry, _ = fleet
    args = {"title": "Implement a fix", "skills": [skill]}
    assert kp.portable_reason(args, cfg) is None
    entry["labels"] = []
    assert kp.portable_reason(args, cfg) == "needs worker coordination capability"


def test_development_title_is_not_cli_execution(fleet):
    cfg, _, _ = fleet
    assert reason("hermes-hub client: retry a 409 from the Board", cfg) is None
