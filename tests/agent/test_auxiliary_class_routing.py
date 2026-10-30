"""Auxiliary classes never enter legacy discovery or paid recovery."""
import asyncio
from unittest.mock import patch

import pytest
from agent import auxiliary_client as aux
from agent.model_routing import ModelRoutingError
from tests.tui_gateway import test_class_routing as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_auxiliary_native_wire(mode, routing_home, streaming_route, native_transport):
    info = {}
    kwargs = dict(model="class:deep", provider="auto", messages=[{"role": "user", "content": "ok"}],
                  main_runtime={"session_id": "aux-owner"}, route_info=info, max_tokens=32)
    with patch.object(aux, "_try_discovery_chain", side_effect=AssertionError("legacy discovery")):
        if mode == "async":
            response = asyncio.run(aux.async_call_llm("title", **kwargs))
        elif mode == "stream":
            chunks = list(aux.call_llm("title", stream=True, **kwargs))
            assert "".join(c.choices[0].delta.content or "" for c in chunks) == "ok"
            response = None
        else:
            response = aux.call_llm("title", **kwargs)
        if response is not None:
            assert response.choices[0].message.content == "ok"
    assert info["requested_class"] == "deep"
    assert info["model"] == "anthropic-deep"
    assert len(native_transport) == 1
    url, headers, body = native_transport[0]
    assert url.startswith("https://api.anthropic.com/")
    assert headers["authorization"].startswith("Bearer sk-ant-oat")
    assert "x-api-key" not in headers
    assert body["model"] == "anthropic-deep"


@pytest.mark.parametrize("async_mode", [False, True])
def test_auxiliary_empty_pool_no_paid_fallback(async_mode, routing_home, streaming_route, monkeypatch):
    monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **k: False)
    with patch.object(aux, "_try_discovery_chain") as discover, patch.object(aux, "_try_payment_fallback") as paid:
        with pytest.raises(ModelRoutingError):
            kwargs = dict(model="class:deep", messages=[{"role": "user", "content": "ok"}])
            if async_mode:
                asyncio.run(aux.async_call_llm("title", **kwargs))
            else:
                aux.call_llm("title", **kwargs)
    discover.assert_not_called()
    paid.assert_not_called()


@pytest.mark.parametrize("override", [{"provider": "anthropic"}, {"base_url": "https://other.invalid"},
                                      {"api_key": "paid"}, {"extra_body": {"model": "paid-model"}},
                                      {"extra_headers": {"Authorization": "Bearer paid"}}])
def test_auxiliary_refuses_transport_bypass(override, routing_home, streaming_route, native_transport):
    with pytest.raises(ModelRoutingError):
        aux.call_llm("title", model="class:deep", messages=[], **override)
    assert native_transport == []


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("failure", [401, 429, "timeout"])
def test_auxiliary_same_class_recovery_on_wire(mode, failure, routing_home, streaming_route,
                                               native_transport, monkeypatch, tmp_path):
    import json
    import httpx
    from copy import deepcopy
    from hermes_cli.config import atomic_config_write
    second = deepcopy(routing_home["model_routing"]["routes"]["deep"][0])
    second["model"] = "z-anthropic-deep"
    routing_home["model_routing"]["routes"]["deep"].append(second)
    routing_home["auxiliary"] = {"title": {"model": "class:deep", "provider": "auto"}}
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    attempts = []
    original = httpx.MockTransport.handle_request

    def respond(transport, request):
        body = json.loads(request.content)
        attempts.append(body["model"])
        if body["model"] == "anthropic-deep":
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(failure, json={"type": "error", "error": {
                "type": "authentication_error" if failure == 401 else "rate_limit_error",
                "message": "synthetic failure"}}, request=request)
        return original(transport, request)

    monkeypatch.setattr(httpx.MockTransport, "handle_request", respond)
    info = {}
    kwargs = dict(messages=[{"role": "user", "content": "ok"}],
                  main_runtime={"session_id": "owner"}, route_info=info)
    def complete():
        if mode == "stream":
            return "".join(chunk.choices[0].delta.content or ""
                           for chunk in aux.call_llm("title", stream=True, **kwargs))
        if mode == "async":
            return asyncio.run(aux.async_call_llm("title", **kwargs)).choices[0].message.content
        return aux.call_llm("title", **kwargs).choices[0].message.content

    with patch.object(aux, "_try_payment_fallback", side_effect=AssertionError("paid bypass")):
        assert complete() == "ok"
        assert info["reason"] == "same-class-failover"
        assert complete() == "ok"
    assert attempts[0] == "anthropic-deep"
    assert attempts[-2:] == ["z-anthropic-deep", "z-anthropic-deep"]
    assert set(attempts) == {"anthropic-deep", "z-anthropic-deep"}
    assert info["requested_class"] == "deep"
    assert all(h["authorization"].startswith("Bearer sk-ant-oat") for _, h, _ in native_transport)


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("failure", [429, 402])
def test_account_recovery_keeps_exact_route_on_wire(mode, failure, routing_home, streaming_route,
                                                    native_transport, monkeypatch, tmp_path):
    import json
    import httpx
    from copy import deepcopy
    from hermes_cli.config import atomic_config_write

    # Paid entry remains first: recovery may only visit the two OAuth accounts.
    auth_path = tmp_path / "auth.json"
    auth = json.loads(auth_path.read_text())
    entries = auth["credential_pool"]["anthropic"]
    first = next(e for e in entries if e["auth_type"] == "oauth")
    second = deepcopy(first)
    second.update(id="second-subscription", label="second-subscription", priority=2,
                  access_token=first["access_token"] + "-second")
    entries.append(second)
    auth_path.write_text(json.dumps(auth))
    routing_home["model_routing"]["enforce_subscription_only"] = True
    routing_home["auxiliary"] = {"title": {"model": "anthropic-deep", "provider": "anthropic"}}
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    attempts = []
    original = httpx.MockTransport.handle_request

    def respond(transport, request):
        body = json.loads(request.content)
        token = request.headers["authorization"]
        attempts.append((body["model"], token))
        if token == "Bearer " + first["access_token"]:
            return httpx.Response(failure, json={"type": "error", "error": {
                "type": "rate_limit_error" if failure == 429 else "billing_error",
                "message": "Rate limit exceeded" if failure == 429 else "credit balance is too low"}}, request=request)
        return original(transport, request)

    monkeypatch.setattr(httpx.MockTransport, "handle_request", respond)
    info = {}
    kwargs = dict(messages=[{"role": "user", "content": "ok"}],
                  main_runtime={"session_id": "account-recovery-owner"}, route_info=info)
    if mode == "stream":
        result = "".join(c.choices[0].delta.content or "" for c in aux.call_llm("title", stream=True, **kwargs))
    elif mode == "async":
        result = asyncio.run(aux.async_call_llm("title", **kwargs)).choices[0].message.content
    else:
        result = aux.call_llm("title", **kwargs).choices[0].message.content
    assert result == "ok"
    assert {model for model, _ in attempts} == {"anthropic-deep"}
    assert attempts[0][1] == "Bearer " + first["access_token"]
    assert attempts[-1][1] == "Bearer " + second["access_token"]
    assert info["reason"] == "allocation"  # account rotation, not route failover
    persisted = json.loads(auth_path.read_text())["credential_pool"]["anthropic"]
    failed_entry = next(e for e in persisted if e["id"] == first["id"])
    if failure == 402:
        assert failed_entry["last_status"] == "exhausted"
    else:
        assert failed_entry.get("model_cooldowns")


@pytest.mark.parametrize("capability", ["vision", "json", "context_tokens"])
def test_auxiliary_capabilities_fail_before_wire(capability, routing_home, streaming_route,
                                                native_transport, tmp_path):
    from hermes_cli.config import atomic_config_write
    caps = routing_home["model_routing"]["routes"]["deep"][0]["capabilities"]
    caps.update(vision=True, json=True)
    caps[capability] = 1 if capability == "context_tokens" else False
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    with pytest.raises(ModelRoutingError):
        aux.call_llm("title", model="class:deep", messages=messages,
                     extra_body={"response_format": {"type": "json_object"}})
    assert native_transport == []
