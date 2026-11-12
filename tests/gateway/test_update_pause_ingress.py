"""Update pauses remain nonterminal through outer turn and canonical admission."""
from unittest.mock import AsyncMock

import pytest

from gateway.session_results import execution_result
from tests.gateway.test_gateway_silence_tokens import _event, _runner, _source


@pytest.mark.asyncio
async def test_outer_pause_preserves_result_without_terminal_side_effects(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    result = {'paused_for_update': True, 'interrupted': True, 'completed': False,
              'final_response': '', 'messages': []}
    runner._run_agent = AsyncMock(return_value=result)
    runner._hmwa_shape_agent_response = AsyncMock(side_effect=AssertionError('terminal shaping on pause'))
    runner._hmwa_post_turn_hooks = AsyncMock()
    runner._hmwa_persist_turn_transcript = AsyncMock()
    runner._deliver_turn_response_then_limit_offer = AsyncMock()
    captured = {}
    token = execution_result.set(captured)
    try:
        response = await runner._handle_message_with_agent(_event(), _source(), 'lane', 1)
    finally:
        execution_result.reset(token)
    assert response is None
    assert captured['result'] is result
    runner._hmwa_shape_agent_response.assert_not_awaited()
    runner._hmwa_post_turn_hooks.assert_not_awaited()
    runner._hmwa_persist_turn_transcript.assert_not_awaited()
    runner._deliver_turn_response_then_limit_offer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_gate', ['allow', 'plugin', 'allowlist', 'estop', 'external_drain', 'missing_session'])
async def test_native_admission_binds_pause_and_resumes_without_replaying_input(monkeypatch, tmp_path, resume_gate):
    import asyncio
    import time
    from agent.turn_iteration_prep import _step_boundary_pause
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_update_pause_hook import installed
    from hermes_cli import admission_fence
    from tests.gateway.test_shared_busy_receive_loop import Adapter, _wait_for
    from tui_gateway.update_pause import PAUSE_REASON, pause_note

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    from hermes_constants import get_process_hermes_home
    tmp_path = get_process_hermes_home()
    runner = GatewayRunner(GatewayConfig())
    adapter = Adapter()
    adapter.gateway_runner, runner.adapters = runner, {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='pause-ingress')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='pause-chat', chat_type='dm', user_id='fixture-user')
    calls, reasons = [], []

    async def turn(event, source, quick_key, run_generation):
        from types import SimpleNamespace
        calls.append(event)
        if len(calls) == 1:
            # Model/provider IO is a stand-in; the completed tool boundary and
            # admitted FIFO/checkpoint/resume/delivery path are production code.
            sid = runner.session_store.get_or_create_session(source).session_id
            history = [
                {'role': 'user', 'content': event.text},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call-1', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'done once', 'tool_call_id': 'call-1', 'tool_name': 'example'},
            ]
            authority.db.append_messages_batch(sid, history)
            admission_fence.set_hold(tmp_path)
            admission_fence.request_pause(tmp_path, expires_at=time.time() + 60)
            agent = SimpleNamespace()
            with installed(agent, message_id=event.message_id):
                reason = _step_boundary_pause(agent, history, 1)
            reasons.append(reason)
            assert reason == PAUSE_REASON
            execution_result.get()['result'] = {'paused_for_update': True, 'interrupted': True,
                                                'final_response': '', 'messages': history}
            return None
        assert event.text == pause_note('original task')
        assert not event.internal and not event.allow_gateway_control
        from gateway.response_filters import display_kind_for_event
        assert display_kind_for_event(event) == 'auto_continue'
        assert event.metadata['gateway_session_strict'] is True
        from gateway.session_contract import SessionRef
        assert event.metadata['gateway_session_id'] == authority.physical_target(
            SessionRef('default', event._claimed_owner[0]))
        assert event._claimed_owner[0] in authority.sessions
        # Exercise production resolution as well as checking the event: an expired
        # idle/daily policy must never allocate a fresh physical transcript.
        monkeypatch.setattr(runner.async_session_store, 'get_or_create_session',
                            AsyncMock(side_effect=AssertionError('resume reset physical session')))
        resolved = await runner._hmwa_resolve_session(event, source)
        if resume_gate == 'missing_session':
            assert resolved is None
            return None
        assert resolved[1].session_id == event.metadata['gateway_session_id']
        assert event.message_id.startswith('update-resume:')
        assert event.media_urls == [] and event.media_types == []
        assert event.auto_skill is None and event.channel_context is None
        assert event.source == calls[0].source
        execution_result.get()['result'] = {'final_response': 'continued', 'messages': []}
        return 'continued'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        await adapter.handle_message(MessageEvent(text='original task', source=source, message_id='original-id'))
        await _wait_for(lambda: bool(reasons), 15)
        assert reasons == [PAUSE_REASON]
        await _wait_for(lambda: bool(authority.update_parked), 15)
        assert not adapter.sent
        assert len(calls) == 1
        async def revoke(event, source):
            monkeypatch.setattr(runner, '_is_user_authorized_for_source', lambda source: False)
            return event

        def stop_gate():
            from agent import estop
            monkeypatch.setattr(estop, 'paused_reply', lambda: 'emergency stop')

        gates = {
            'allow': lambda: None,
            'plugin': lambda: setattr(runner, '_hm_pre_gateway_dispatch_hook', AsyncMock(return_value=None)),
            'allowlist': lambda: setattr(runner, '_hm_pre_gateway_dispatch_hook', AsyncMock(side_effect=revoke)),
            'estop': stop_gate,
            'external_drain': lambda: setattr(runner, '_external_drain_active', True),
            'missing_session': lambda: monkeypatch.setattr(
                runner.async_session_store, 'lookup_by_session_key', AsyncMock(return_value=None)),
        }
        gates[resume_gate]()
        admission_fence.clear_hold(tmp_path)
        admission_fence.clear_pause_request(tmp_path)
        if resume_gate != 'allow':
            await _wait_for(lambda: all(live.task.done() for live in authority.sessions.values()), 15)
            assert len(calls) == (2 if resume_gate == 'missing_session' else 1), 'resume bypassed current ingress policy'
            if resume_gate == 'missing_session':
                with authority.db._read_ctx() as conn:
                    outcomes = conn.execute('SELECT outcome FROM session_admissions').fetchall()
                assert [row[0] for row in outcomes] == ['failed']
            if resume_gate == 'plugin':
                runner._hm_pre_gateway_dispatch_hook.assert_awaited_once()
            return
        await _wait_for(lambda: adapter.sent == ['continued'] and not adapter._background_tasks, 15)
        assert len(calls) == 2
        assert not authority.update_parked
    finally:
        admission_fence.clear_hold(tmp_path)
        admission_fence.clear_pause_request(tmp_path)
        for task in [*(live.task for live in authority.sessions.values() if live.task), *adapter._background_tasks]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(live.task for live in authority.sessions.values() if live.task),
                             *adapter._background_tasks, return_exceptions=True)
        authority.db.close()


@pytest.mark.asyncio
async def test_native_admission_without_pause_is_unaffected_and_delivers_once(monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_update_pause_hook import current_scope
    from tests.gateway.test_shared_busy_receive_loop import Adapter, _wait_for

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    runner = GatewayRunner(GatewayConfig())
    adapter = Adapter()
    adapter.gateway_runner, runner.adapters = runner, {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='nopause')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='plain-chat', chat_type='dm', user_id='fixture-user')
    seen = []

    async def turn(event, source, quick_key, run_generation):
        seen.append((event.text, event.internal, event.message_id, current_scope(event.message_id).eligible))
        execution_result.get()['result'] = {'final_response': 'done', 'messages': []}
        return 'done'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        await adapter.handle_message(MessageEvent(text='hello', source=source, message_id='m1'))
        await _wait_for(lambda: adapter.sent == ['done'] and not adapter._background_tasks, 15)
        assert seen == [('hello', False, 'm1', True)] and not authority.update_parked
    finally:
        authority.db.close()


@pytest.mark.parametrize('payload, platform, expected', [
    ({'native_text_v1': {}}, 'telegram', True),
    ({'text': 'x'}, 'local', True),
    ({'text': 'x'}, 'telegram', False),
    ({'native_text_v1': {}, 'finite': True}, 'local', False),
    ({'api_turn_v1': {}}, 'local', False),
    ({'local_automation_v1': {}}, 'local', False),
])
def test_only_native_or_local_chat_admissions_are_pause_eligible(payload, platform, expected):
    from types import SimpleNamespace
    from gateway.session_update_pause_ingress import supported
    live = SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value=platform)))
    assert supported(None, {'payload': payload}, None, live, is_api=False) is expected
    assert supported(None, {'payload': payload}, None, live, is_api=True) is False


def test_resume_event_drops_original_replay_and_keeps_route():
    from gateway.platforms.event import MessageEvent
    from gateway.session_update_pause_ingress import resume_event
    from tests.gateway.test_gateway_silence_tokens import _source
    from tui_gateway.update_pause import pause_note
    original = MessageEvent(text='do it', source=_source(), message_id='orig', media_urls=['/x.png'],
                            media_types=['image/png'], auto_skill='s', metadata={'k': 1})
    assert resume_event({'update_resume': None}, original) is original
    resumed = resume_event({'admission_id': 'a', 'update_resume': {'admission_id': 'a'}}, original)
    assert (resumed.text, resumed.source, resumed.message_id, resumed.media_urls, resumed.auto_skill,
            resumed.internal, resumed.allow_gateway_control, resumed.metadata) == (
        pause_note('do it'), original.source, 'update-resume:a', [], None, False, False, {})
