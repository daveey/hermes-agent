"""Carbon is an explicitly requested, funded class, never ordinary capacity."""
from types import SimpleNamespace

import pytest

from agent.model_routing import ModelRoutingError, eligible_routes, exact_class_selector, resolve_model_route
from agent.model_routing_managed import verify_selected_route
from tests.agent.test_model_routing import policy


def carbon_config(billing="metered"):
    config = policy()
    config["model_routing"]["enforce_subscription_only"] = True
    config["model_routing"]["routes"]["carbon"] = [{
        "provider": "gemini", "model": "gemini-4-carbon", "billing": billing,
        "weight": 1, "eligible": True, "funding_verified": True,
        "capabilities": {"tools": True, "vision": True, "json": True, "streaming": True},
    }]
    return config


@pytest.mark.parametrize("billing", ["metered", "credit-funded"])
def test_explicit_carbon_resolves_and_passes_outbound_policy(tmp_path, billing):
    config = carbon_config(billing)
    assert exact_class_selector("class:carbon")
    result = resolve_model_route("class:carbon", config=config, session_id="explicit",
                                 state_path=tmp_path / "routes.db")
    assert (result["provider"], result["model"]) == ("gemini", "gemini-4-carbon")
    assert result["metadata"]["requested_class"] == "carbon"
    verify_selected_route(result["model"], result["provider"], result["metadata"], config)


@pytest.mark.parametrize("level", ["standard", "deep", "fast"])
@pytest.mark.parametrize("allow_metered", [False, True])
def test_carbon_cannot_be_ordinary_capacity_or_fallback(tmp_path, level, allow_metered):
    config = carbon_config()
    policy = config["model_routing"]
    policy.update(allow_metered=allow_metered, enforce_subscription_only=False)
    paid = policy["routes"]["carbon"][0]
    policy["routes"][level].append(paid)  # even an accidentally duplicated registration cannot spend
    assert paid not in eligible_routes(policy, level)
    args = dict(config=config, state_path=tmp_path / "routes.db", session_id=level)
    assert resolve_model_route("class:" + level, **args)["provider"] != "gemini"
    excluded = [(r["provider"], r["model"]) for r in policy["routes"][level] if r != paid]
    with pytest.raises(ModelRoutingError, match="eligible"):
        resolve_model_route("class:" + level, excluded=excluded, **args)
    with pytest.raises(ModelRoutingError):
        resolve_model_route("gemini-4-carbon", "gemini", config=carbon_config())


@pytest.mark.parametrize("key", ["eligible", "funding_verified"])
def test_carbon_still_requires_attestation(key):
    config = carbon_config()
    config["model_routing"]["routes"]["carbon"][0][key] = False
    assert eligible_routes(config["model_routing"], "carbon") == []


def test_carbon_physical_request_passes_both_policy_fences(tmp_path, monkeypatch):
    from agent.model_routing_request import validate_class_request
    from hermes_cli import config_effective
    config = carbon_config()
    config["model_routing"]["routes"]["carbon"][0]["capabilities"]["context_tokens"] = 1_048_576
    monkeypatch.setattr(config_effective, "load_user_config_effective", lambda: config)
    result = resolve_model_route("class:carbon", config=config, session_id="physical",
                                 state_path=tmp_path / "routes.db")
    agent = SimpleNamespace(model=result["model"], provider=result["provider"],
                            model_routing=result["metadata"], api_key="fixture-key", api_mode="chat_completions",
                            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                            _credential_pool=None, _model_routing_requirements={})
    request = {"model": agent.model, "messages": [{"role": "user", "content": "carbon ok"}]}
    validate_class_request(agent, request, streaming=False)
    config["model_routing"]["routes"]["carbon"][0]["funding_verified"] = False
    with pytest.raises(ModelRoutingError):
        validate_class_request(agent, request, streaming=False)


def test_carbon_delegation_inherits_only_requested_class():
    from agent.kanban_delegation_policy import parent_class
    assert parent_class(SimpleNamespace(model_routing={"requested_class": "carbon"})) == "class:carbon"
    assert parent_class(SimpleNamespace(model="gemini-4-carbon")) == "class:standard"


def test_managed_carbon_fence_rejects_spoofed_class_or_identity(tmp_path):
    config = carbon_config()
    result = resolve_model_route("class:carbon", config=config, session_id="explicit",
                                 state_path=tmp_path / "routes.db")
    for change in ({"requested_class": "standard"}, {"billing_group": "subscription"},
                   {"selected_model": "other"}):
        with pytest.raises(ModelRoutingError):
            verify_selected_route(result["model"], result["provider"], {**result["metadata"], **change}, config)
