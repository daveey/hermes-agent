"""The transmitting SDK, not its saved runtime snapshot, owns the auth proof."""
from unittest.mock import patch

import pytest

from agent import auxiliary_class_routing as routing
from agent import auxiliary_client as aux
from agent.model_routing import ModelRoutingError
from tests.agent import test_managed_auxiliary as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


def corrupt_credential(leaf):
    leaf.api_key = "synthetic-paid"
    leaf.auth_token = None


def corrupt_endpoint(leaf):
    leaf.base_url = "https://paid.invalid/v1"


def corrupt_headers(leaf):
    leaf._custom_headers["x-api-key"] = "synthetic-paid"


@pytest.mark.parametrize("mutation", [corrupt_credential, corrupt_endpoint, corrupt_headers])
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.asyncio
async def test_mutated_sdk_never_sends(mutation, mode, native_transport):
    original = routing._client

    def build(runtime):
        client = original(runtime)
        mutation(client._real_client)
        return client

    options = dict(model="class:deep", messages=[{"role": "user", "content": "ok"}])
    with patch.object(routing, "_client", side_effect=build):
        with pytest.raises(ModelRoutingError):
            if mode == "async":
                await aux.async_call_llm(**options)
            elif mode == "stream":
                list(aux.call_llm(**options, stream=True))
            else:
                aux.call_llm(**options)
    assert native_transport == []


@pytest.mark.parametrize("mutation", [
    {"model": "paid"}, {"extra_headers": {"x-api-key": "synthetic-paid"}},
    {"extra_body": {"model": "paid"}},
])
def test_relay_rewrite_cannot_bypass_final_send(mutation, native_transport):
    from agent import relay_llm

    def rewrite(kwargs, callback, **options):
        return callback({**kwargs, **mutation})

    with patch.object(aux, "_relay_auxiliary_metadata", return_value=(
            "anthropic", "anthropic-deep", {"auxiliary_task": "test"})), \
            patch.object(relay_llm, "execute_current", side_effect=rewrite):
        with pytest.raises(ModelRoutingError):
            aux.call_llm(model="class:deep", messages=[{"role": "user", "content": "ok"}])
    assert native_transport == []
