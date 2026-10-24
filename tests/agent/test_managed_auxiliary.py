"""Managed native auxiliary pins remain exact through deferred SDK calls."""
from unittest.mock import patch

import pytest

from agent import auxiliary_client as aux
from agent.model_routing import ModelRoutingError
from hermes_cli.config import atomic_config_write
from tests.tui_gateway import test_class_routing as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


@pytest.fixture(autouse=True)
def managed(routing_home, streaming_route, tmp_path):
    routing_home['model_routing']['enforce_subscription_only'] = True
    atomic_config_write(tmp_path / 'config.yaml', routing_home)


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('model', ['class:deep', 'anthropic-deep'])
@pytest.mark.asyncio
async def test_managed_deferred_native_send(async_mode, model, native_transport):
    client, selected = aux.resolve_provider_client('auto', model, async_mode=async_mode)
    assert selected == model
    result = client.chat.completions.create(model=selected, messages=[{'role': 'user', 'content': 'ok'}])
    if async_mode:
        result = await result
    assert result.choices[0].message.content == 'ok'
    assert native_transport[0][2]['model'] == 'anthropic-deep'
    assert native_transport[0][1]['authorization'].startswith('Bearer sk-ant-oat')
    assert 'x-api-key' not in native_transport[0][1]


@pytest.mark.parametrize('kind', ['sync', 'async', 'stream', 'direct', 'vision'])
@pytest.mark.asyncio
async def test_managed_paid_override_never_sends(kind, native_transport):
    options = dict(provider='openrouter', model='paid', messages=[{'role': 'user', 'content': 'ok'}])
    calls = {
        'sync': lambda: aux.call_llm(**options),
        'stream': lambda: list(aux.call_llm(**options, stream=True)),
        'direct': lambda: aux.resolve_provider_client('openrouter', 'paid'),
        'vision': lambda: aux.resolve_vision_provider_client(provider='openrouter', model='paid'),
    }
    with patch.object(aux, '_try_discovery_chain', side_effect=AssertionError('paid discovery')):
        with pytest.raises(ModelRoutingError):
            if kind == 'async':
                await aux.async_call_llm(**options)
            else:
                calls[kind]()
    assert native_transport == []
