"""Main native transmission validates the live SDK after Relay rewrites."""
import json
from unittest.mock import patch

import anthropic
import httpx
import pytest

from run_agent import AIAgent
from tests.agent import test_managed_subscription as fixtures
from tests.agent.test_class_failure_wire import completion

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('mutation', ['none', 'credential', 'endpoint', 'headers', 'relay-model', 'relay-auth'])
def test_main_native_wire_rechecks_live_client(managed, stream, mutation):
    from agent import relay_llm
    sent = []
    original = anthropic.Anthropic

    def respond(request):
        sent.append(request)
        body = json.loads(request.content)
        if body.get('stream'):
            return completion(body['model'])
        return httpx.Response(200, json={'id': 'message', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': [{'type': 'text', 'text': 'ok'}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}})

    def sdk(**kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)
        client = original(**kwargs)
        mutations = {
            'credential': lambda: setattr(client, 'auth_token', 'synthetic-paid'),
            'endpoint': lambda: setattr(client, 'base_url', 'https://paid.invalid'),
            'headers': lambda: client._custom_headers.update({'Authorization': 'Bearer synthetic-paid'}),
        }
        mutations.get(mutation, lambda: None)()
        return client

    original_stream, original_execute = relay_llm.stream, relay_llm.execute

    def rewrite(kwargs):
        changes = {'relay-model': {'model': 'unapproved'},
                   'relay-auth': {'extra_headers': {'Authorization': 'Bearer synthetic-paid'}}}
        return {**kwargs, **changes.get(mutation, {})}

    def relay_stream(kwargs, callback, **options):
        return original_stream(kwargs, lambda final: callback(rewrite(final)), **options)

    def relay_execute(kwargs, callback, **options):
        return original_execute(kwargs, lambda final: callback(rewrite(final)), **options)

    with patch('anthropic.Anthropic', side_effect=sdk), \
         patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.title_generator._auto_title_enabled', return_value=False), \
         patch('agent.turn_recovery.interruptible_backoff_sleep', return_value=None), \
         patch.object(relay_llm, 'stream', side_effect=relay_stream), \
         patch.object(relay_llm, 'execute', side_effect=relay_execute):
        agent = AIAgent(model='anthropic-deep', provider='anthropic', session_id='wire',
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        agent._disable_streaming = not stream
        try:
            result = agent.run_conversation('Say ok')
            if mutation == 'none':
                assert result['final_response'] == 'ok', result
                assert sent
            else:
                assert result.get('failed') or result.get('error'), result
                assert sent == []
        finally:
            agent.release_clients()
