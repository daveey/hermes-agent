"""Real paused agent result must not launch or consume legacy follow-up work."""
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import admission_fence
from tests.gateway.test_pending_steer_drain import Adapter
from tests.gateway.test_turn_update_pause_real_agent import _drive


@pytest.mark.asyncio
@pytest.mark.parametrize('internal', [False, True])
async def test_real_pause_preserves_pending_fifo_until_resumed_turn_finishes(tmp_path, monkeypatch, internal):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent, paused, _ = _drive(tmp_path, request=True)
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    adapter = Adapter(PlatformConfig(enabled=True), Platform.LOCAL)
    source = SessionSource(platform=Platform.LOCAL, chat_id='chat', user_id='u', chat_type='dm')
    key = build_session_key(source)
    events = [MessageEvent(text=f'pending {i}', source=source, message_id=f'id-{i}',
                           internal=internal, metadata={'sentinel': i}) for i in range(2)]
    for event in events:
        runner._enqueue_fifo(key, event, adapter)
    admission_fence.set_hold(tmp_path)
    try:
        assert await runner._run_agent_drain_pending(paused, adapter, source, key) == (None, None)
        assert runner._queue_depth(key, adapter=adapter) == 2
        assert agent.client.chat.completions.create.call_count == 1
    finally:
        admission_fence.clear_hold(tmp_path)
    # An old paused result is not permission to run; the resumed turn must complete first.
    assert await runner._run_agent_drain_pending(paused, adapter, source, key) == (None, None)
    for original in events:
        event, text = await runner._run_agent_drain_pending({'final_response': 'done'}, adapter, source, key)
        assert event is original and text == original.text
    assert runner._queue_depth(key, adapter=adapter) == 0
    agent._session_db.close()
