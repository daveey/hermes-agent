"""Codex/xAI native credentials and final Relay request are fenced on wire."""
import json
from copy import deepcopy
from unittest.mock import patch

import httpx
import openai
import pytest

from agent.model_routing import ModelRoutingError
from hermes_cli.config import atomic_config_write
from run_agent import AIAgent
from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.fixture(params=['openai-codex', 'xai-oauth'])
def native_policy(request, managed, tmp_path):
    provider = request.param
    route = deepcopy(managed['model_routing']['routes']['deep'][0])
    route.update(provider=provider, model=provider + '-deep')
    managed['model_routing']['routes']['deep'] = [route]
    atomic_config_write(tmp_path / 'config.yaml', managed)
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {provider: [
        {'id': 'native', 'label': 'native', 'priority': 1, 'auth_type': 'oauth',
         'source': 'manual', 'access_token': 'synthetic-oauth-token'}]}}))
    return provider, route['model']


def response(request):
    model = json.loads(request.content)['model']
    item = {'type': 'message', 'id': 'm1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'ok', 'annotations': []}]}
    final = {'id': 'r1', 'object': 'response', 'model': model, 'status': 'completed', 'output': [item],
             'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
    events = [{'type': 'response.created', 'response': {**final, 'status': 'in_progress', 'output': []}},
              {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
              {'type': 'response.completed', 'response': final}]
    body = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events)
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)


@pytest.mark.parametrize('mutation', ['none', 'credential', 'headers', 'relay-auth'])
def test_responses_main_wire(native_policy, mutation):
    from agent import process_bootstrap, relay_llm
    sent = []
    original = openai.OpenAI

    def respond(request):
        sent.append(request)
        return response(request)

    def sdk(**kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)
        client = original(**kwargs)
        mutations = {'credential': lambda: setattr(client, 'api_key', 'synthetic-paid'),
                     'headers': lambda: client._custom_headers.update({'Authorization': 'Bearer synthetic-paid'})}
        mutations.get(mutation, lambda: None)()
        return client

    original_stream = relay_llm.stream

    def relay_stream(kwargs, callback, **options):
        def final(request):
            if mutation == 'relay-auth':
                request = {**request, 'extra_headers': {'Authorization': 'Bearer synthetic-paid'}}
            return callback(request)
        return original_stream(kwargs, final, **options)

    provider, model = native_policy
    with patch.object(process_bootstrap, 'OpenAI', side_effect=sdk), \
         patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.title_generator._auto_title_enabled', return_value=False), \
         patch('agent.turn_recovery.interruptible_backoff_sleep', return_value=None), \
         patch.object(relay_llm, 'stream', side_effect=relay_stream):
        agent = AIAgent(model=model, provider=provider, session_id='responses',
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        try:
            result = agent.run_conversation('Say ok')
            if mutation == 'none':
                assert result['final_response'] == 'ok', result
                assert sent
                assert all(r.headers['authorization'] == 'Bearer synthetic-oauth-token' for r in sent)
            else:
                assert result.get('failed') or result.get('error'), result
                assert sent == []
        finally:
            agent.release_clients()


@pytest.mark.parametrize('invalid', [False, True])
def test_native_refresh_keeps_pool_provenance(native_policy, invalid):
    provider, model = native_policy
    with patch('model_tools.get_tool_definitions', return_value=[]):
        agent = AIAgent(model=model, provider=provider, session_id='refresh',
                        quiet_mode=True, skip_memory=True, skip_context_files=True)
    entry = deepcopy(agent._credential_pool.current())
    entry.access_token = 'synthetic-refreshed'
    if invalid:
        entry.auth_type = 'api_key'
    try:
        with patch.object(agent._credential_pool, 'try_refresh_current', return_value=entry), \
             patch.object(agent._credential_pool, 'current', return_value=entry), \
             patch.object(agent, '_adopt_openai_credentials', return_value=True) as adopt, \
             patch('hermes_cli.auth.resolve_codex_runtime_credentials') as codex, \
             patch('hermes_cli.auth.resolve_xai_oauth_runtime_credentials') as xai:
            if invalid:
                with pytest.raises(ModelRoutingError):
                    agent._try_refresh_codex_client_credentials()
                adopt.assert_not_called()
            else:
                assert agent._try_refresh_codex_client_credentials()
                assert adopt.call_args.args[0] == 'synthetic-refreshed'
            codex.assert_not_called()
            xai.assert_not_called()
    finally:
        agent.release_clients()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['sync', 'async', 'stream'])
async def test_auxiliary_responses_rejects_mutated_leaf(native_policy, mode):
    from agent import auxiliary_client as aux, auxiliary_class_routing as routing
    original = routing._client

    def corrupt(runtime):
        client = original(runtime)
        client._real_client._custom_headers['Authorization'] = 'Bearer synthetic-paid'
        return client

    options = {'model': 'class:deep', 'messages': [{'role': 'user', 'content': 'ok'}]}
    with patch.object(routing, '_client', side_effect=corrupt), patch('httpx.Client.send') as send:
        with pytest.raises(ModelRoutingError):
            if mode == 'async':
                await aux.async_call_llm(**options)
            elif mode == 'stream':
                list(aux.call_llm(**options, stream=True))
            else:
                aux.call_llm(**options)
        send.assert_not_called()
