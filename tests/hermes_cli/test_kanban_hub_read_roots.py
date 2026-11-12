"""A declared, attested ``hub:read`` clears hub-read's own reads: ``ssh hermes-hub <sub>`` and paths under its roots."""
import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_coordination as coord
from tests.hermes_cli import test_kanban_coordination_placement as helpers

fleet = helpers.fleet

ROOTS = ["logs", "cron", "kanban", "cache/core-update", "core-update", "cache/hermes-sync", "cache/delegation",
         "config.yaml", "coach", "user-steps", "inbox"]
STEP = "Step 1: ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log and report the matches."


@pytest.fixture(autouse=True)
def _board_maps_access(monkeypatch):
    monkeypatch.setattr(coord, "BOARD_PLACEABLE_ACCESS", frozenset(coord.ACCESS_LABELS))


@pytest.fixture
def attested(fleet):
    cfg, entry, machines = fleet
    entry["labels"].append("capability:hub-read")
    cfg["hub_read_roots"] = list(ROOTS)
    return cfg, entry, machines


def reason(body, cfg, requires=("hub:read",)):
    return kp.portable_reason({"title": "Check the sync log", "body": body, "requires": list(requires)}, cfg)


def test_declared_read_step_is_portable(attested):
    cfg, _, _ = attested
    assert reason(STEP, cfg) is None


@pytest.mark.parametrize("body", [
    "ssh hermes-hub cat /home/ubuntu/.hermes/config.yaml",
    "ssh hermes-hub tail ~/.hermes/cron/output/x.log",
    "ssh hermes-hub ls ~/.hermes/cache/core-update/",
    "ssh hermes-hub sqlite kanban 'select 1'",
    "read `~/.hermes/kanban/boards/x/kanban.db`.",
])
def test_other_hub_read_shapes_are_portable(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg) is None


def test_undeclared_card_still_pins(attested):
    cfg, _, _ = attested
    assert reason(STEP, cfg, requires=()) == "home gateway marker"


def test_pool_without_label_still_pins(attested):
    cfg, entry, _ = attested
    entry["labels"].remove("capability:hub-read")
    assert reason(STEP, cfg) is not None


def test_unset_roots_fail_closed(attested):
    cfg, _, _ = attested
    del cfg["hub_read_roots"]
    assert reason(STEP, cfg) == "home gateway marker"
    assert reason("ssh hermes-hub grep X", cfg) is None  # the reader invocation alone needs no roots


@pytest.mark.parametrize("roots", ["logs", ["/"], [".."], ["logs/../state"], [""], [None]])
def test_malformed_roots_clear_nothing(attested, roots):
    cfg, _, _ = attested
    cfg["hub_read_roots"] = roots
    assert reason(STEP, cfg) == "home gateway marker"


@pytest.mark.parametrize("body", [
    "ssh hermes-hub cat ~/.hermes/state/x",
    "ssh hermes-hub cat ~/.hermes/logs/../state.db",
    "ssh hermes-hub cat ~/.hermes/logsx/y",
    "ssh mini grep X ~/.hermes/logs/hermes-sync.log",
    "ssh hermes-hub sudo reboot",
    "ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log, then restart the gateway",
    "ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log, then systemctl restart hermes-sync",
    "ssh hermes-hub cat ~/.hermes/config.yaml and hermes config set kanban.x 1",
])
def test_controls_still_pin(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg) == "home gateway marker"


def test_claim_refusal_unchanged(attested):
    _, entry, _ = attested
    card = {"placement_policy": "worker-first-v1", "title": "t", "body": STEP, "requires": ["hub:read"]}
    assert coord.claim_refusal(card, "worker") is None
    entry["labels"].remove("capability:hub-read")
    assert coord.claim_refusal(card, "worker") == coord.REASON
