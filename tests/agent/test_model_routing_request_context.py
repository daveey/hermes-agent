"""validate_class_request compares TOKEN estimates (not bytes) to token caps, per request."""
from types import SimpleNamespace

import pytest

from agent.model_routing import ModelRoutingError
from agent.model_routing_request import validate_class_request


def _route(**caps):
    return {"provider": "anthropic", "model": "claude-haiku-5-5", "billing": "subscription",
            "eligible": True, "funding_verified": True, "weight": 1,
            "capabilities": {"tools": True, "streaming": True, "context_tokens": 200000, **caps}}


@pytest.fixture
def setup(monkeypatch):
    policy = {"enabled": True, "policy_version": 1, "allow_metered": False,
              "routes": {"fast": [_route()]}}
    monkeypatch.setattr("agent.model_routing_managed.verify_managed_agent", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective",
                        lambda *a, **k: {"model_routing": policy})
    monkeypatch.setattr("agent.model_routing_request.validate_policy", lambda p: None)
    agent = SimpleNamespace(
        model="claude-haiku-5-5", provider="anthropic", max_tokens=None,
        model_routing={"selected_model": "claude-haiku-5-5", "selected_provider": "anthropic",
                       "requested_class": "fast", "billing_group": "subscription"},
        _model_routing_requirements={"tools": True, "streaming": True, "min_context_tokens": 64000})

    def request(chars, **extra):
        return {"model": "claude-haiku-5-5", "messages": [
            {"role": "user", "content": "x" * chars}], "max_tokens": 4096, **extra}
    return agent, request


def test_bytes_over_cap_but_tokens_under_cap_passes(setup):
    agent, request = setup
    req = request(600_000)  # ~150k tokens, 600KB > 200000
    validate_class_request(agent, req, streaming=True)
    assert 100_000 < agent._model_routing_requirements["min_context_tokens"] < 200_000


def test_genuinely_over_context_fails_and_names_capability(setup):
    agent, request = setup
    with pytest.raises(ModelRoutingError, match=r"failing capability: min_context_tokens\(required=\d+, attested=200000\)") as exc:
        validate_class_request(agent, request(900_000), streaming=True)
    assert "xxxx" not in str(exc.value)


def test_requirement_shrinks_after_compression_and_floor_kept(setup):
    agent, request = setup
    validate_class_request(agent, request(600_000), streaming=True)
    with pytest.raises(ModelRoutingError):
        validate_class_request(agent, request(900_000), streaming=True)
    validate_class_request(agent, request(4_000), streaming=True)
    assert agent._model_routing_requirements["min_context_tokens"] == 64000
    agent.model_routing["_"] = 1
    agent._model_routing_context_floor = 250_000  # construction floor above cap still fails closed
    with pytest.raises(ModelRoutingError, match="min_context_tokens"):
        validate_class_request(agent, request(10), streaming=True)


@pytest.mark.parametrize("extra,key", [
    ({"tools": None}, "tools"),
    ({"response_format": {"type": "json_object"}}, "json"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]}, "vision"),
])
def test_capability_mismatch_still_fails_closed_and_is_named(setup, extra, key):
    agent, request = setup
    req = request(10)
    req.update(extra)
    if key == "tools":
        agent._model_routing_requirements.pop("tools")
        req["tools"] = []
        agent.model_routing["x"] = 1
        # tools absent on request -> not required; make the route lack the capability instead
        req["tools"] = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
        import hermes_cli.config_effective as ce
        orig = ce.load_user_config_effective
        ce.load_user_config_effective = lambda *a, **k: {"model_routing": {
            "enabled": True, "policy_version": 1, "allow_metered": False,
            "routes": {"fast": [_route(tools=False)]}}}
        try:
            with pytest.raises(ModelRoutingError, match="failing capability: .*tools"):
                validate_class_request(agent, req, streaming=True)
        finally:
            ce.load_user_config_effective = orig
        return
    with pytest.raises(ModelRoutingError, match=f"failing capability: .*{key}"):
        validate_class_request(agent, req, streaming=True)
