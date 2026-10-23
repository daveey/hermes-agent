"""Class routing preserves class, funding, pins and durable identity."""
from concurrent.futures import ProcessPoolExecutor
from collections import Counter

import pytest

from agent.model_routing import ModelRoutingError, resolve_model_route, validate_policy


def policy():
    return {"model_routing": {
        "enabled": True, "policy_version": 1, "allow_metered": False,
        "routes": {level: [
            {"provider": provider, "model": f"{provider}-{level}",
             "billing": "subscription", "weight": 1, "eligible": True,
             "funding_verified": True,
             "capabilities": {"tools": True, "vision": True, "context_tokens": 200000}}
            for provider in ("anthropic", "openai-codex", "xai-oauth")
        ] for level in ("deep", "standard", "fast")}}}


def allocate(args):
    home, index = args
    return resolve_model_route("class:deep", config=policy(), session_id=str(index),
                               state_path=home)["provider"]


def test_concurrent_provider_balance(tmp_path):
    path = str(tmp_path / "routes.db")
    with ProcessPoolExecutor(max_workers=4) as pool:
        counts = Counter(pool.map(allocate, [(path, i) for i in range(60)]))
    assert counts == {"anthropic": 20, "openai-codex": 20, "xai-oauth": 20}


@pytest.mark.parametrize("level", ["deep", "standard", "fast"])
def test_class_failover_and_sticky_restart_lineage(tmp_path, level):
    args = dict(config=policy(), state_path=tmp_path / "routes.db")
    first = resolve_model_route(f"class:{level}", session_id="original", **args)
    assert first["model"].endswith(level)
    for _ in range(4):
        assert resolve_model_route(f"class:{level}", session_id="original", **args) == first
    child = resolve_model_route(f"class:{level}", session_id="compressed",
                                parent_session_id="original", **args)
    assert child == first
    second = resolve_model_route(f"class:{level}", session_id="compressed",
        excluded=[(first["provider"], first["model"])], **args)
    assert second["provider"] != first["provider"]
    assert second["model"].endswith(level)
    assert resolve_model_route(f"class:{level}", session_id="original", **args) == second


def test_unknown_explicit_pin_unchanged_and_disabled(tmp_path):
    assert resolve_model_route("special/unknown", "custom", config={}) == {
        "provider": "custom", "model": "special/unknown", "metadata": None}
    with pytest.raises(ModelRoutingError, match="disabled"):
        resolve_model_route("class:deep", config={}, session_id="a", state_path=tmp_path / "r.db")
    with pytest.raises(ModelRoutingError, match="session"):
        resolve_model_route("class:deep", config=policy())


def test_context_and_capabilities_fail_closed(tmp_path):
    for requirements in ({"min_context_tokens": 200001}, {"json": True}):
        with pytest.raises(ModelRoutingError, match="eligible"):
            resolve_model_route("class:deep", config=policy(), session_id="a",
                                requirements=requirements, state_path=tmp_path / "r.db")


def test_subscription_before_paid_and_no_unapproved_fallback(tmp_path):
    cfg = policy()
    routes = cfg["model_routing"]["routes"]["deep"]
    paid = dict(routes[0], provider="gemini", model="paid-deep", billing="credit-funded")
    routes.insert(0, paid)
    args = dict(config=cfg, session_id="a", state_path=tmp_path / "r.db")
    chosen = resolve_model_route("class:deep", **args)
    assert chosen["provider"] != "gemini"
    excluded = [(r["provider"], r["model"]) for r in routes if r != paid]
    with pytest.raises(ModelRoutingError, match="eligible"):
        resolve_model_route("class:deep", excluded=excluded, **args)
    cfg["model_routing"]["allow_metered"] = True
    result = resolve_model_route("class:deep", excluded=excluded, **args)
    assert result["provider"] == "gemini"
    assert result["metadata"]["billing_group"] == "credit-funded"


def test_extra_models_do_not_buy_provider_share(tmp_path):
    cfg = policy()
    routes = cfg["model_routing"]["routes"]["deep"]
    routes.append(dict(routes[0], model="another-deep"))
    counts = Counter(resolve_model_route("class:deep", config=cfg, session_id=str(i),
                    state_path=tmp_path / "r.db")["provider"] for i in range(30))
    assert set(counts.values()) == {10}


@pytest.mark.parametrize("provider", ["gemini", "openai-api", "xai"])
def test_false_subscription_rejected(provider):
    cfg = policy()["model_routing"]
    cfg["routes"]["deep"][0]["provider"] = provider
    with pytest.raises(ModelRoutingError, match="subscription"):
        validate_policy(cfg)


def test_unverified_funding_and_disabled_route_excluded(tmp_path):
    cfg = policy()
    for route in cfg["model_routing"]["routes"]["deep"]:
        route["funding_verified"] = False
    with pytest.raises(ModelRoutingError, match="eligible"):
        resolve_model_route("class:deep", config=cfg, session_id="a", state_path=tmp_path / "r.db")
