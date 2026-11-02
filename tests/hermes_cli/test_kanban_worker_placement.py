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
    {"title": "Read ~/code/example"}, {"title": "Inspect kanban.db"},
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
    assert "placement_policy" not in placement.decide({"title": "Inspect ~/code/repo"}, cfg=cfg, self_gw="hub")
