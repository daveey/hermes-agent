"""Readiness probes must not allocate durable side jobs or enter paid discovery."""
from unittest.mock import patch

import pytest

from agent import auxiliary_client as aux
from agent.model_routing import ModelRoutingError
from tests.tui_gateway import test_class_routing as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


@pytest.mark.parametrize('task', ['goal_judge', 'compression', ''])
def test_class_readiness_and_deferred_request(task, routing_home, streaming_route, native_transport, tmp_path):
    from hermes_cli.config import atomic_config_write
    routing_home['model'].update(default='class:deep', provider='auto')
    routing_home['auxiliary'] = {task: {'model': 'class:deep', 'provider': 'auto'}}
    atomic_config_write(tmp_path / 'config.yaml', routing_home)
    with patch.object(aux, '_try_discovery_chain', side_effect=AssertionError('legacy discovery')):
        client, model = aux.get_text_auxiliary_client(task, main_runtime={'session_id': 'judge-owner'})
        assert client is not None
        assert native_transport == []
        assert not (tmp_path / 'model-routing.sqlite3').exists()
        response = client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': 'ok'}], max_tokens=32)
    assert response.choices[0].message.content == 'ok'
    assert native_transport[0][2]['model'] == 'anthropic-deep'
    assert native_transport[0][1]['authorization'].startswith('Bearer sk-ant-oat')


def test_class_readiness_exhaustion_never_discovers_paid(routing_home, streaming_route, monkeypatch):
    monkeypatch.setattr('agent.credential_pool.CredentialPool.has_available', lambda *a, **k: False)
    with patch.object(aux, '_try_discovery_chain') as discovery:
        with pytest.raises(ModelRoutingError):
            aux.get_text_auxiliary_client('goal_judge', main_runtime={'requested_class': 'deep', 'session_id': 'owner'})
    discovery.assert_not_called()


@pytest.mark.parametrize('override', ['model', 'vision', 'context'])
def test_readiness_does_not_authorize_later_bypass(override, routing_home, streaming_route, native_transport, tmp_path):
    from hermes_cli.config import atomic_config_write
    routing_home['model_routing']['routes']['deep'][0]['capabilities']['vision'] = False
    atomic_config_write(tmp_path / 'config.yaml', routing_home)
    client, model = aux.get_text_auxiliary_client('title', main_runtime={'requested_class': 'deep'})
    kwargs = dict(model=model, messages=[{'role': 'user', 'content': 'ok'}])
    if override == 'model':
        kwargs['model'] = 'paid-model'
    elif override == 'vision':
        kwargs['messages'] = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}]
    else:
        kwargs['max_tokens'] = 10000000
    with pytest.raises(ModelRoutingError):
        client.chat.completions.create(**kwargs)
    client.close()
    assert native_transport == []
