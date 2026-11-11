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


def test_execution_rechecks_already_imported_card(fleet, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_placement as wp
    _, entry, _ = fleet
    monkeypatch.setattr(sync, "board_mode", lambda _: "primary")
    monkeypatch.setattr(sync, "sync_state", lambda _: {"gateway_id": "worker"})
    card = {"title": "cross_task read", "placement_policy": "worker-first-v1",
            "runs_on": '["machine:worker"]'}
    assert wp.execution_refusal(None, card) is None
    entry["labels"] = []
    assert wp.execution_refusal(None, card) == "needs worker coordination capability"


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


@pytest.mark.parametrize("text", [
    "hermes-hub client: retry a 409 from the Board",
    "Build a `hermes-hub` CLI shim",
    "Fix the `cross_task` endpoint",
    "Live check on\nhermes-hub reports no tools and exits 0",
    "crash storm on hermes-hub) was detected yesterday",
    "hermes-hub error receipt PR75",
    "hosts/hermes-hub/bin/hub-core-update",
    'connection="hermes-hub"',
    "Fix cross_task comments being labeled human through Desktop endpoint",
    "Check cross_task comment179201 wrongly writer_kind=human",
    "Test actual agent cross_task path and human UI path independently",
])
def test_development_or_hostname_mentions_add_no_requirement(fleet, text):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, entry, _ = fleet
    entry["labels"] = []
    assert coord.requirements(text) == (set(), None)
    assert reason(text, cfg) is None


@pytest.mark.parametrize("text", [
    "Run hermes-hub frobnicate", "`hermes-hub frobnicate`",
    "hermes-hub unknown-command", "hermes-hub --new-option",
    "hermes-hub status; hermes-hub create-task title",
    "Use cross_task comment", "cross_task comment task-id",
    'cross_task(action="comment", target="task")',
    "cross_task read; cross_task unknown-command", "cross_task",
])
def test_explicit_execution_is_fail_closed(fleet, text):
    from hermes_cli import kanban_placement_coordination as coord
    assert coord.requirements(text)[1] == coord.ACTION_REASON


@pytest.mark.parametrize("text", ["hermes-hub status", "hermes-hub pull", "cross_task read", "cross_task list"])
def test_each_read_needs_fresh_attestation(fleet, text):
    cfg, entry, _ = fleet
    assert reason(text, cfg) is None
    entry["labels"] = []
    assert reason(text, cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("pools", [None, [], "worker", {}, {"hub": [" "]},
                                        {"hub": ["worker"] * 65}])
def test_missing_or_invalid_current_pool(fleet, pools):
    cfg, _, _ = fleet
    cfg["worker_pools"] = pools
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("snapshot", [None, [], {}, {"machines": None},
                                           {"machines": []}, {"machines": {}},
                                           {"machines": {"worker": None}}])
def test_invalid_registry_or_missing_destination(fleet, monkeypatch, snapshot):
    cfg, _, _ = fleet
    monkeypatch.setattr(wn, "registry", lambda: snapshot)
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


@pytest.mark.parametrize("field,value", [
    ("labels", None), ("labels", [1, "capability:coordination-read"]),
    ("last_seen", None), ("last_seen", float("nan")),
    ("last_seen", 10**1000), ("last_seen", float("-inf")),
])
def test_malformed_attestation_fields(fleet, field, value):
    cfg, entry, _ = fleet
    entry[field] = value
    assert reason("cross_task read", cfg) == "needs worker coordination capability"


def test_explicit_destination_does_not_borrow_current_pool(fleet):
    from hermes_cli import kanban_placement_coordination as coord
    cfg, _, _ = fleet
    assert coord.check("cross_task read", cfg, target="missing") == coord.REASON
    cfg["worker_pools"] = None
    assert coord.check("cross_task read", cfg, target="worker") is None


def test_marker_exemption_preserves_legacy_and_other_text():
    from hermes_cli import kanban_placement_coordination as coord
    text = "hermes-hub status; hermes-hub sessions; token-broker; ssh server"
    assert coord.marker_text(text) == "; hermes-hub sessions; ; ssh server"


@pytest.mark.parametrize("command", [
    "sync", "sessions", "inbox", "tasks", "where", "links", "request", "requests",
    "inspect-legacy", "reconcile-legacy", "inspect-alias", "retire-alias",
    "adopt-task", "create-task", "depend", "outcomes", "install-watch", "watch", "diff-shared",
])
def test_every_legacy_cli_marker_still_refuses(fleet, command):
    cfg, _, _ = fleet
    assert reason("hermes-hub " + command, cfg) == "needs home coordination action"
    # The old marker permits arbitrary whitespace; narrowing intent detection
    # must never turn a non-exempt old marker into a portable card.
    assert reason("hermes-hub\n" + command, cfg) is not None


def test_ordinary_work_does_not_read_registry(monkeypatch):
    from hermes_cli import kanban_placement_coordination as coord
    def unexpected():
        pytest.fail("ordinary source work must not need registry availability")
    monkeypatch.setattr(wn, "registry", unexpected)
    assert coord.check("Fix cross_task comments in the endpoint", {}) is None
    assert coord.legacy_refusal({"title": "Fix cross_task comments"}) is False
