"""Direct client resolution must retain the class fence until physical send."""
from unittest.mock import patch

import pytest

from agent import auxiliary_client as aux
from agent.model_routing import ModelRoutingError
from tests.tui_gateway import test_class_routing as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


@pytest.mark.parametrize('vision', [False, True])
@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.asyncio
async def test_direct_class_native_send(vision, async_mode, routing_home, streaming_route, native_transport, tmp_path):
    options = dict(main_runtime={'requested_class': 'deep', 'session_id': 'direct-owner'}, async_mode=async_mode)
    with patch.object(aux, '_try_discovery_chain', side_effect=AssertionError('paid discovery')):
        if vision:
            provider, client, model = aux.resolve_vision_provider_client(**options)
            assert provider == 'auto'
        else:
            client, model = aux.resolve_provider_client('auto', **options)
        assert not (tmp_path / 'model-routing.sqlite3').exists()
        result = client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': 'ok'}], max_tokens=32)
        if async_mode:
            result = await result
        assert result.choices[0].message.content == 'ok'
    assert native_transport[0][2]['model'] == 'anthropic-deep'
    assert native_transport[0][1]['authorization'].startswith('Bearer sk-ant-oat')


@pytest.mark.parametrize('options', [
    {'raw_codex': True}, {'explicit_api_key': 'unapproved-key'},
    {'explicit_base_url': 'https://unapproved.invalid'}, {'api_mode': 'chat_completions'},
])
def test_direct_class_rejects_transport_bypass(options, routing_home, streaming_route, native_transport):
    with pytest.raises(ModelRoutingError):
        aux.resolve_provider_client('auto', 'class:deep', **options)
    assert native_transport == []


def test_direct_vision_requires_capability_before_discovery(routing_home, streaming_route, native_transport, tmp_path):
    from hermes_cli.config import atomic_config_write
    routing_home['model_routing']['routes']['deep'][0]['capabilities']['vision'] = False
    atomic_config_write(tmp_path / 'config.yaml', routing_home)
    with patch.object(aux, '_vision_auto_route') as legacy:
        with pytest.raises(ModelRoutingError):
            aux.resolve_vision_provider_client(model='class:deep')
    legacy.assert_not_called()
    assert native_transport == []


def test_direct_explicit_pin_remains_legacy(routing_home, streaming_route):
    sentinel = object()
    with patch.object(aux, '_resolve_registry_branch', return_value=(sentinel, 'specialized')):
        client, model = aux.resolve_provider_client('special-provider', 'specialized', main_runtime={'requested_class': 'deep'})
    assert client is sentinel
    assert model == 'specialized'


@pytest.mark.parametrize('option', ['json', 'context', 'unknown', 'exhausted'])
def test_direct_send_rechecks_request(option, routing_home, streaming_route, native_transport, tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    routing_home['model_routing']['routes']['deep'][0]['capabilities']['json'] = False
    atomic_config_write(tmp_path / 'config.yaml', routing_home)
    client, model = aux.resolve_provider_client('auto', 'class:deep')
    kwargs = dict(model=model, messages=[{'role': 'user', 'content': 'ok'}])
    if option == 'json':
        kwargs['response_format'] = {'type': 'json_object'}
    elif option == 'context':
        kwargs['max_completion_tokens'] = 10000000
    elif option == 'unknown':
        kwargs['unsupported_sdk_option'] = True
    else:
        monkeypatch.setattr('agent.credential_pool.CredentialPool.has_available', lambda *a, **k: False)
    with patch.object(aux, '_try_discovery_chain') as discovery:
        with pytest.raises((ModelRoutingError, TypeError)):
            client.chat.completions.create(**kwargs)
    discovery.assert_not_called()
    assert native_transport == []
