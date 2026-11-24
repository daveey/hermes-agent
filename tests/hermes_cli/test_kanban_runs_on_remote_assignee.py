"""A card whose ``runs_on`` excludes this machine is not refused over THIS machine's profiles. [daveey]

10-31 19:12 PDT: a MacBook worker called ``kanban_create(assignee="bb-tuner", placement="hub",
requires=[terminal, file, git])``. ``placement`` became ``runs_on=[machine:hub]``, then ``create_task`` ran the
local ``validate_assignment`` against the MacBook's profiles and refused ("Profile 'bb-tuner' does not exist"),
although ``bb-tuner`` exists on the hub, where the card would run.

For such a card the requirement check reads the landing machines' published ``profile-tools`` records
(:func:`kanban_runs_on_capabilities.check_create`), the skills and model-pin checks defer to the landing machine,
and :func:`kanban_runs_on_capabilities.claim_refusal` stays the pickup backstop. Cards that can run here keep
today's local checks.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_capabilities import CapabilityMismatchError
from tests.hermes_cli.test_kanban_requires_placement import WITH_KANBAN, _publish
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway

pytest_plugins = ["tests.hermes_cli.test_kanban_requires_placement"]  # its ``gws`` and ``svc`` fixtures

PROFILE = "bb-tuner"
NEEDS = ["terminal", "file", "git"]
SKILL = "bb-tuning"


def _profile(home, name=PROFILE, config=WITH_KANBAN):
    target = home / "profiles" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.yaml").write_text(config)
    return target


def _enable_profile_routing(home):
    """Supply the actual isolated executing profile's eligible subscription registry."""
    from hermes_cli.config_effective import load_profile_config_effective
    from hermes_cli.kanban_model_assignment import _assignee_config
    path = home / "profiles" / PROFILE / "config.yaml"
    config = load_profile_config_effective(path, fail_closed=True)
    config["model_routing"] = load_profile_config_effective(home / "config.yaml", fail_closed=True)["model_routing"]
    path.write_text(json.dumps(config))
    assert _assignee_config(PROFILE)["model_routing"] == config["model_routing"]


def _count(conn):
    return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


# --- 1. the landing machine's published tools cover the needs ---------------------------------------------------

def test_remote_profile_covering_the_needs_is_created(gws, monkeypatch):
    hub, _ = gws
    _publish(monkeypatch, {WORKER: {PROFILE: {"terminal", "file", "git"}}})
    tid = kb.create_task(hub.conn, title="tune", assignee=PROFILE, requires=NEEDS, runs_on=[f"machine:{WORKER}"])
    task = kb.get_task(hub.conn, tid)
    assert task.assignee == PROFILE
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        f"machine:{WORKER}"]


# --- 2. the landing machine's published tools lack one ---------------------------------------------------------

def test_remote_profile_lacking_a_tool_is_refused_naming_the_machine(gws, monkeypatch):
    from hermes_cli.kanban_runs_on_capabilities import PlacementCapabilityMismatch
    hub, _ = gws
    _publish(monkeypatch, {WORKER: {PROFILE: {"file"}}})
    before = _count(hub.conn)
    with pytest.raises(PlacementCapabilityMismatch) as caught:
        kb.create_task(hub.conn, title="tune", assignee=PROFILE, requires=NEEDS, runs_on=[f"machine:{WORKER}"])
    msg = str(caught.value)
    assert caught.value.code == "runs_on_capability_mismatch"
    assert WORKER in msg and "terminal" in msg, msg
    assert "does not exist" not in msg, msg  # not the local-profile error
    assert _count(hub.conn) == before


# --- 3. no remote record: created, and the claim gate still guards pickup ------------------------------------

def test_remote_tools_unknown_creates_and_claim_gate_still_refuses(gws, svc, monkeypatch):
    hub, worker = gws
    _publish(monkeypatch, {})
    tid = kb.create_task(hub.conn, title="tune", assignee=PROFILE, requires=NEEDS, runs_on=[f"machine:{WORKER}"])
    hub.round()
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None, "worker-1 copied a card for a profile it does not have"
    assert tid not in svc.board(BOARD)["leases"]
    from hermes_cli import kanban_runs_on_capabilities as roc
    worker.enter()
    assert "does not exist" in (roc.claim_refusal({"id": tid, "assignee": PROFILE, "requires": json.dumps(NEEDS)})
                                or "")
    # Control: once worker-1 has the profile, it takes the card.
    _profile(worker.home)
    worker.round()
    assert kb.get_task(worker.conn, tid) is None  # pickup refuses before importing an unroutable profile
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_model_tier import class_unroutable_reason
    config = _assignee_config(PROFILE)
    assert class_unroutable_reason("class:standard", config) == "routing_disabled"
    _enable_profile_routing(worker.home)
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER


# --- 4. a card that can run here keeps today's local checks --------------------------------------------------

@pytest.mark.parametrize("runs_on", [None, ["linux"], [f"machine:{HUB}"]])
def test_card_that_can_run_here_is_still_refused_for_a_missing_local_profile(gws, monkeypatch, runs_on):
    hub, _ = gws
    _publish(monkeypatch, {WORKER: {PROFILE: {"terminal", "file", "git"}}})
    before = _count(hub.conn)
    with pytest.raises(CapabilityMismatchError) as caught:
        kb.create_task(hub.conn, title="tune", assignee=PROFILE, requires=NEEDS, runs_on=runs_on)
    assert caught.value.code == "assignee_capability_mismatch"
    assert "does not exist" in str(caught.value)
    assert _count(hub.conn) == before


def test_remote_card_still_validates_the_requires_shape(gws, monkeypatch):
    hub, _ = gws
    _publish(monkeypatch, {})
    with pytest.raises(ValueError):
        kb.create_task(hub.conn, title="tune", assignee=PROFILE, requires=["bogus"], runs_on=[f"machine:{WORKER}"])


# --- 5a. pinned skills ---------------------------------------------------------------------------------------

def test_remote_card_skill_check_does_not_read_the_local_profile(gws, svc, monkeypatch):
    from hermes_cli.kanban_skill_check import UnknownSkillError
    hub, worker = gws
    _publish(monkeypatch, {WORKER: {PROFILE: {"terminal", "file", "git"}}})
    _profile(hub.home)  # the hub has a bb-tuner too, without the skill
    hub.enter()
    # Control: a card that runs here is refused for the skill this machine's profile lacks.
    with pytest.raises(UnknownSkillError):
        kb.create_task(hub.conn, title="here", assignee=PROFILE, requires=NEEDS, skills=[SKILL], runs_on=["linux"],
                       validate_skills=True)
    tid = kb.create_task(hub.conn, title="there", assignee=PROFILE, requires=NEEDS, skills=[SKILL],
                         runs_on=[f"machine:{WORKER}"], validate_skills=True)
    assert kb.get_task(hub.conn, tid).skills == [SKILL]
    # Deferred to the landing machine, which is not stranded by it: its bb-tuner lacks the skill too and still
    # takes the card; its worker starts with the missing-skill note (kanban_skill_check.missing_pin_note).
    _profile(worker.home)
    hub.round()
    worker.round()
    assert kb.get_task(worker.conn, tid) is None  # pickup refuses before importing an unroutable profile
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_model_tier import class_unroutable_reason
    config = _assignee_config(PROFILE)
    assert class_unroutable_reason("class:standard", config) == "routing_disabled"
    _enable_profile_routing(worker.home)
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER


# --- 5b. model pin -------------------------------------------------------------------------------------------

def test_remote_card_model_pin_is_not_checked_against_the_local_profile(gws, monkeypatch):
    """Neutral policy is global; placement cannot turn a named model into valid policy."""
    from hermes_cli.config_effective import load_profile_config_effective
    hub, _ = gws
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "git"}}})
    policy = load_profile_config_effective(hub.home / "config.yaml", fail_closed=True)
    assert policy["kanban"]["sync"]["enabled"] and policy["model_routing"]["enabled"]
    for runs_on in (None, [f"machine:{WORKER}"]):
        before = _count(hub.conn)
        for model, provider in [("claude-sonnet-4-5", "anthropic"), ("gpt-5", "anthropic")]:
            with pytest.raises(ValueError, match="model-neutral"):
                kb.create_task(hub.conn, title="bad", assignee="default", requires=NEEDS,
                               runs_on=runs_on, model_override=model, provider_override=provider)
            assert _count(hub.conn) == before
        tid = kb.create_task(hub.conn, title="class route", assignee="default", requires=NEEDS,
                             runs_on=runs_on, model_override="class:deep")
        task = kb.get_task(hub.conn, tid)
        assert (task.model_override, task.provider_override) == ("class:deep", "auto")


def test_remote_card_provider_less_pin_is_refused_naming_the_provider(gws, monkeypatch):
    hub, _ = gws
    _publish(monkeypatch, {WORKER: {PROFILE: {"terminal", "file", "git"}}})
    # Local credentials cannot authorize a concrete selector, here or remotely.
    monkeypatch.setattr("hermes_cli.models_detect.provider_has_credentials", lambda provider: provider == "anthropic")
    for assignee, runs_on in (("default", None), (PROFILE, [f"machine:{WORKER}"])):
        before = _count(hub.conn)
        for provider in (None, "anthropic"):
            with pytest.raises(ValueError, match="model-neutral"):
                kb.create_task(hub.conn, title="bad", assignee=assignee, requires=NEEDS,
                               runs_on=runs_on, model_override="claude-opus-5-5", provider_override=provider)
            assert _count(hub.conn) == before
        tid = kb.create_task(hub.conn, title="class route", assignee=assignee, requires=NEEDS,
                             runs_on=runs_on, model_override="class:deep")
        task = kb.get_task(hub.conn, tid)
        assert (task.model_override, task.provider_override) == ("class:deep", "auto")


# --- 6. the tool entry path ----------------------------------------------------------------------------------

def test_tool_create_with_placement_on_an_enrolled_machine_creates_the_card(tmp_path, monkeypatch, svc):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_sync as sync
    from tests.hermes_cli.test_kanban_worker_node import _tool_create
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    mac = Gateway(tmp_path, "macbook", svc, monkeypatch, pickup=False)
    try:
        machines = {**MACHINES, HUB: {**MACHINES[HUB], "pickup": "explicit"}}
        wn.set_registry_fetch(lambda: {"me": "macbook", "machines": list(machines.values())})
        _publish(monkeypatch, {HUB: {PROFILE: {"terminal", "file", "git"}}})
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        out = _tool_create({
            "title": "design CI capacity", "assignee": PROFILE, "placement": HUB, "requires": NEEDS,
            "model": "class:deep", "provider": "auto"})
        assert not out.get("error"), out
        tid = out["task_id"]
        assert out.get("runs_on") == [f"machine:{HUB}"], out
        row = mac.conn.execute("SELECT assignee, runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row[0] == PROFILE and json.loads(row[1]) == [f"machine:{HUB}"]
    finally:
        mac.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)
