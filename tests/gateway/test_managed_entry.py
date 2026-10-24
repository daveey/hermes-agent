"""Managed paid overrides at actual HTTP and messaging ingress."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


from gateway.session import Platform, SessionSource
from tests.agent import test_managed_subscription as fixtures
from tests.gateway import test_api_class_routing as api_fixtures
from tests.gateway.test_pre_agent_fallback_notice import _runner_with_real_runtime_resolution
from tests.waiting import async_wait_until

adapter = api_fixtures.adapter
managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_paid_completions_never_send(managed, adapter, stream):
    app = web.Application()
    app.router.add_post('/v1/chat/completions', adapter._handle_chat_completions)
    with patch('httpx.Client.send') as send, patch('model_tools.get_tool_definitions', return_value=[]):
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/v1/chat/completions', json={
                'model': 'vendor/paid', 'provider': 'openrouter', 'stream': stream,
                'messages': [{'role': 'user', 'content': 'Say ok'}]})
            text = await response.text()
            # The existing OpenAI facade renders auth errors as assistant content,
            # including SSE errors after headers have been sent.
            assert 'Managed subscription policy rejects' in text
        send.assert_not_called()


@pytest.mark.asyncio
async def test_paid_run_never_sends(managed, adapter):
    app = web.Application()
    app.router.add_post('/v1/runs', adapter._handle_runs)
    with patch('httpx.Client.send') as send, patch('model_tools.get_tool_definitions', return_value=[]):
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/v1/runs', json={
                'model': 'vendor/paid', 'provider': 'openrouter', 'input': 'Say ok'})
            payload = await response.json()
            assert response.status == 202, payload
            await async_wait_until(lambda: not adapter.active_agent_work_count(), timeout=20)
            status = adapter._run_statuses[payload['run_id']]
            assert status['status'] == 'failed', status
        send.assert_not_called()


def test_saved_messaging_paid_override_rejected(managed):
    runner = _runner_with_real_runtime_resolution()
    runner._peek_session_state.return_value = SimpleNamespace(conversation=SimpleNamespace(model_override={
        'model': 'vendor/paid', 'provider': 'openrouter'}))
    with patch('gateway.run._resolve_gateway_model', return_value='class:deep'), \
         patch('gateway.run._get_channel_override', return_value=None), patch('httpx.Client.send') as send:
        with pytest.raises(RuntimeError, match='Managed subscription policy'):
            runner._resolve_session_agent_runtime(
                source=SessionSource(platform=Platform.LOCAL, chat_id='c'), user_config=managed)
        send.assert_not_called()
