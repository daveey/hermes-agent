"""Frozen worker-first policy: conservative portable-card boundary."""
import pytest

from hermes_cli import kanban_placement as placement


@pytest.mark.parametrize("title", ["Summarize a public paper", "Run the full test suite"])
def test_portable_reason_accepts_light_and_heavy_scratch_work(title):
    assert placement.portable_reason({"title": title, "requires": ["terminal", "file"]}, {}) is None


@pytest.mark.parametrize("fields", [
    {"workspace_kind": "dir"}, {"workspace_kind": "worktree"}, {"workspace_path": "/checkout"},
    {"board": "another"}, {"assignee": "unlisted"}, {"project": "project"},
    {"requires": ["host_repo"]}, {"requires": ["ssh:host"]}, {"requires": ["unknown"]},
    {"needs": ["ssh:host"]},
    {"title": "Edit ~/.hermes/config.yaml"}, {"title": "Inspect kanban.db"},
    {"title": "Tail /home/ubuntu/.hermes/logs/agent.log"},
    {"title": "Restart the gateway"}, {"title": "Use token-broker"},
    {"title": "Run launchd"}, {"title": "Inspect Chrome on my Mac"},
    {"human_request": {"action": "approve"}}, {"max_retries": 2}, {"tenant": "x"},
])
def test_portable_reason_retains_conservative_exclusions(fields):
    assert placement.portable_reason({"title": "Analyze public data", **fields}, {})
    args = {"title": "Analyze public data", **fields}
    cfg = {"route": {"macbook": "hub"}, "heavy_route": {"macbook": "legacy"}}
    options = {"self_gw": "macbook", "live": lambda _: None, "carries": True,
               "locate": lambda _: {"holders": [("hub", {})]}}
    legacy = placement.decide(args, cfg=cfg, **options)
    pooled = placement.decide(args, cfg={**cfg, "worker_pools": {"macbook": ["worker-1"]}}, **options)
    assert pooled == legacy


def test_extra_home_marker_remains_an_exclusion():
    assert placement.portable_reason({"title": "Inspect special-device"}, {"extra_home_markers": ["special-device"]})


def test_parent_and_origin_metadata_are_not_mutated():
    args = {"title": "Analyze public data", "parents": ["parent"], "origin_session": "@session:default@gw/id"}
    before = dict(args)
    assert placement.portable_reason(args, {}) is None
    assert args == before


@pytest.mark.parametrize("pool", [[], ["worker-1"], ["worker-1", "worker-2"]])
@pytest.mark.parametrize("title", ["Summarize public data", "Run full test suite"])
def test_pool_precedes_gateway_and_heavy_routes_and_never_falls_back(pool, title):
    cfg = {"worker_pools": {"hub": pool}, "route": {"hub": "other"}, "heavy_route": {"hub": "old-worker"}}
    result = placement.decide({"title": title}, cfg=cfg, self_gw="hub")
    assert result["where"] == "local"  # storage stays here; execution is fenced
    assert result["placement_policy"] == "worker-first-v1"
    assert result["why"] == "waiting_for_worker_capacity"
    assert result.get("runs_on") is None


@pytest.mark.parametrize("fields", [{"placement": "local"}, {"placement": "hub"}, {"runs_on": []}, {"runs_on": ["linux"]}])
def test_explicit_pins_bypass_worker_policy(fields):
    result = placement.decide({"title": "Analyze data", **fields}, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert "placement_policy" not in result
    if "runs_on" in fields:
        args = {"title": "Analyze data", **fields}
        cfg = {"route": {"macbook": "hub"}}
        options = {"self_gw": "macbook", "live": lambda _: None, "carries": True,
               "locate": lambda _: {"holders": [("hub", {})]}}
        assert placement.decide(args, cfg={**cfg, "worker_pools": {"macbook": ["worker-1"]}}, **options) == placement.decide(args, cfg=cfg, **options)


@pytest.mark.parametrize("fields", [{"title": "Fix the hermes-hub ssh config"}, {"requires": ["ssh:hub"]}])
def test_hub_bound_work_keeps_legacy_route(fields):
    args = {"title": "Analyze data", **fields}
    cfg = {"route": {"macbook": "hub"}}
    options = {"self_gw": "macbook", "live": lambda _: None, "carries": True,
               "locate": lambda _: {"holders": [("hub", {})]}}
    legacy = placement.decide(args, cfg=cfg, **options)
    assert legacy["where"] == "hub"
    assert placement.decide(args, cfg={**cfg, "worker_pools": {"macbook": ["worker-1"]}}, **options) == legacy


def test_courier_and_host_work_bypass_worker_policy():
    cfg = {"worker_pools": {"hub": ["worker-1"]}}
    assert "placement_policy" not in placement.decide({"title": "Analyze data"}, origin_intent=object(), cfg=cfg, self_gw="hub")
    assert "placement_policy" not in placement.decide({"title": "Inspect ~/code/repo", "requires": ["host_repo"]},
                                                      cfg=cfg, self_gw="hub")


@pytest.mark.parametrize("title", [
    "Fix flaky test in ~/code/hermes/tests/test_x.py",
    "Patch /home/ubuntu/code/fabric-play and open a PR",
    "Branch off ~/.hermes/hermes-agent: git -C ~/.hermes/hermes-agent log origin/main",
    "Update the skill at ~/.hermes/skills/devops/foo/SKILL.md in hermes-mods",
    "hermes-hub client: retry a 409 from the Board",
    "Read the hermes-agent tree under ~/.hermes for context",
])
def test_ordinary_repo_work_enrolls_worker_first(title):
    """Repo paths, the Board CLI name and a bare ~/.hermes are not host needs: workers provision checkouts."""
    assert placement.portable_reason({"title": title, "requires": ["terminal", "file", "git"]}, {}) is None
    result = placement.decide({"title": title, "requires": ["terminal", "file", "git"]},
                              cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert result["placement_policy"] == "worker-first-v1"


@pytest.mark.parametrize("title", [
    "Edit ~/.hermes/config.yaml on this gateway", "Rotate /home/ubuntu/.hermes/.env",
    "Prune ~/.hermes/kanban/workspaces", "Fix the hermes-hub ssh config", "Restart the gateway",
    "Continue in worktree ~/code/_wt/fix-x and push", "cd /home/ubuntu/code/_wt/foo then pytest",
])
def test_hub_state_work_stays_local(title):
    result = placement.decide({"title": title}, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert "placement_policy" not in result



def test_placement_note_from_the_origin_is_not_a_host_need():
    """A card the MacBook placed on the hub carries remote_body's note naming Chrome, launchd and ssh as
    the MacBook's own things; the note must not make the card look host-bound on the hub."""
    args = {"title": "Fix flaky kanban test", "requires": ["terminal", "file", "git"]}
    args["body"] = placement.remote_body({"body": "Deflake tests/x.py and open a PR."}, self_gw="macbook", target="hub")
    assert "Chrome" in args["body"] and "launchd" in args["body"]
    assert placement.portable_reason(args, {}) is None
    result = placement.decide(args, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert result["placement_policy"] == "worker-first-v1"


def test_placement_note_does_not_hide_a_real_host_need_in_the_card_text():
    args = {"title": "Fix flaky kanban test"}
    args["body"] = placement.remote_body({"body": "Reproduce with launchctl list on the Mac."},
                                         self_gw="macbook", target="hub")
    assert placement.portable_reason(args, {}) == "local marker"
