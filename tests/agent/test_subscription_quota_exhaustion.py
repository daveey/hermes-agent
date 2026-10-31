"""An exhausted OAuth pool remains a quota wall, never a paid-auth accusation."""
import json
import time
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
def test_success_then_pool_exhaustion_retains_quota_and_pin(managed, stream):
    sent = []
    reset = time.time() + 7200
    original = anthropic.Anthropic

    def respond(request):
        sent.append(request)
        body = json.loads(request.content)
        if len(sent) > 1:
            return httpx.Response(429, json={'type': 'error', 'error': {
                'type': 'rate_limit_error', 'message': 'Subscription usage limit reached',
                'resets_at': reset}})
        if body.get('stream'):
            return completion(body['model'])
        return httpx.Response(200, json={'id': 'message', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': [{'type': 'text', 'text': 'ok'}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}})

    def sdk(**kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)
        return original(**kwargs)

    with patch('anthropic.Anthropic', side_effect=sdk), \
         patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.title_generator._auto_title_enabled', return_value=False), \
         patch('agent.turn_api_error.interruptible_backoff_sleep', return_value=None), \
         patch('agent.turn_recovery.interruptible_backoff_sleep', return_value=None):
        agent = AIAgent(model='anthropic-deep', provider='anthropic', session_id='quota-wire',
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        agent._disable_streaming = not stream
        try:
            assert agent.run_conversation('Say ok')['final_response'] == 'ok'
            result = agent.run_conversation('Say ok again')
            assert agent._credential_pool.current() is None
            assert result.get('failure_reason') == 'rate_limit', result
            assert result.get('failure_retryable') is True
            assert result.get('failure_resets_at') == pytest.approx(reset, rel=0, abs=0.01)
            assert 'paid API key' not in result.get('final_response', '')
            assert agent.model == 'anthropic-deep'
            assert agent.model_routing['exact_pin'] is True
            assert agent._fallback_chain == []
        finally:
            agent.release_clients()
    assert len(sent) == 2
    assert all(r.headers.get('authorization', '').startswith('Bearer sk-ant-oat') for r in sent)
    assert all('x-api-key' not in r.headers for r in sent)
    assert all(json.loads(r.content)['model'] == 'anthropic-deep' for r in sent)


def test_pinned_startup_preserves_quota_type_and_deadline(managed):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.runtime_provider import resolve_runtime_provider, resolve_runtime_with_fallback
    from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code

    runtime = resolve_runtime_provider(requested='anthropic', target_model='anthropic-deep',
                                       routing_session_id='before-quota')
    reset = time.time() + 7200
    pool = runtime['credential_pool']
    assert pool.mark_exhausted_and_rotate(status_code=429, model='anthropic-deep',
        credential_id='subscription', error_context={'reset_at': reset}, failure_reason='rate_limit') is None
    with pytest.raises(SubscriptionQuotaUnavailable) as caught:
        resolve_runtime_with_fallback(managed, requested='anthropic', target_model='anthropic-deep',
                                      routing_session_id='pinned-start')
    assert caught.value.resets_at == pytest.approx(reset, rel=0, abs=0.01)
    assert caught.value.model == 'anthropic-deep'
    assert turn_exit_code(None, kanban_worker=True, **credential_failure_flags(caught.value)) == 75
    wrapped = RuntimeError('agent init failed')
    wrapped.__cause__ = caught.value
    assert turn_exit_code(None, kanban_worker=True, **credential_failure_flags(wrapped)) == 75


@pytest.mark.parametrize('key', ['synthetic-paid', 'sk-ant-oat-foreign'])
def test_exhaustion_does_not_hide_foreign_runtime_credential(managed, key):
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_runtime import verify_class_runtime
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested='anthropic', target_model='anthropic-deep')
    runtime['credential_pool'].mark_exhausted_and_rotate(status_code=429, model='anthropic-deep',
        credential_id='subscription', error_context={'reset_at': time.time() + 7200})
    runtime['api_key'] = key
    with pytest.raises(ModelRoutingError) as caught:
        verify_class_runtime(runtime, runtime['model_routing'])
    assert not isinstance(caught.value, SubscriptionQuotaUnavailable)


def test_subscription_deadline_ignores_paid_pool_rows(managed):
    from agent.credential_pool import load_pool
    from agent.model_routing_unavailable import quota_unavailable
    pool = load_pool('anthropic', allowed_auth_types={'oauth'})
    now = time.time()
    pool.mark_exhausted_and_rotate(status_code=429, model='anthropic-deep',
        credential_id='paid', error_context={'reset_at': now + 1800})
    pool.mark_exhausted_and_rotate(status_code=429, model='anthropic-deep',
        credential_id='subscription', error_context={'reset_at': now + 7200})
    assert quota_unavailable(pool, 'anthropic-deep').resets_at == pytest.approx(now + 7200, rel=0, abs=0.01)
