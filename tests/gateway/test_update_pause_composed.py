"""Controlled provider/tool IO; real local ingress, handler, TurnRunner and durable resume.

Unlike the helper-level tests, neither the handler nor the pause/checkpoint/resume
machinery is replaced. Only credential resolution, agent allocation and external IO
are controlled. The tool dispatcher and conversation loop remain real.
"""
import asyncio
import copy
import threading
import time

import pytest

from tests.agent.test_step_boundary_pause import _final_response, _make_agent, _tool_response


@pytest.mark.asyncio
async def test_local_admission_real_agent_parks_and_resumes_without_tool_replay(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_contract import Submission
    from gateway.session_local import create_local_session
    from hermes_cli import admission_fence
    from hermes_constants import get_process_hermes_home
    from hermes_state_runtime import get_session_admission
    from hermes_state_update_pause import get_session_checkpoint
    from tests.waiting import async_wait_until
    from tui_gateway.update_pause import pause_note

    home = get_process_hermes_home()
    runner = GatewayRunner(GatewayConfig())
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='composed-pause')
    connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    ref = create_local_session(authority, connection.actor, {
        'request_id': 'composed-local', 'source': 'tui', 'cwd': str(tmp_path),
        'model': 'test/model', 'toolsets': []})
    await authority.attach(connection.actor, ref, history=False)
    physical = authority.physical_target(ref)
    started, release = threading.Event(), threading.Event()
    effects, provider_inputs, turns = [], [], []
    agent = _make_agent([])
    # Restore AIAgent's actual dispatcher; the shared fixture normally overrides it.
    del agent._execute_tool_calls
    agent.session_id, agent._session_db = physical, authority.db
    agent._save_trajectory = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None

    def provider(**kwargs):
        provider_inputs.append(copy.deepcopy(kwargs['messages']))
        return _tool_response(1) if len(provider_inputs) == 1 else _final_response()

    agent.client.chat.completions.create.side_effect = provider

    def tool(name, args, *a, **k):
        assert name == 'web_search' and args == {'query': 'x'}
        started.set()
        assert release.wait(10), 'test never released running tool'
        effects.append('effect-once')
        return 'ok'

    def allocate(turn, *a, **k):
        turns.append((turn._ctx.session_id, turn._ctx.message, turn._ctx.persist_user_display_kind))
        turn._ctx.agent_holder[0] = agent
        return agent, len(turns) > 1

    monkeypatch.setattr(TurnRunner, '_resolve_turn_agent', allocate)
    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', lambda **k: ('test/model', {}))
    monkeypatch.setattr('model_tools.handle_function_call', tool)
    try:
        receipt = await authority.submit(connection.actor, Submission(
            'composed-original', ref, {'text': 'do some tool work'}, 'queue'))
        await async_wait_until(started.is_set, timeout=15)
        admission_fence.set_hold(home)
        admission_fence.request_pause(home, expires_at=time.time() + 60)
        # A running tool must not become a persisted checkpoint until it returns.
        assert not authority.update_parked
        assert effects == []
        release.set()
        await async_wait_until(lambda: bool(authority.update_parked), timeout=15)
        parked = get_session_checkpoint(authority.db, session_id=ref.session_id)
        assert parked and parked['admission_id'] == receipt.admission_id
        before = get_session_admission(authority.db, admission_id=receipt.admission_id)
        assert before['status'] == 'started' and before['outcome'] is None
        assert len(provider_inputs) == 1 and effects == ['effect-once']
        stored = authority.db.get_messages(physical)
        assert [m['role'] for m in stored] == ['user', 'assistant', 'tool']
        assert stored[-1]['tool_call_id'] == 'call_1' and stored[-1]['content'] == 'ok'
        assert len(turns) == 1

        admission_fence.clear_hold(home)
        admission_fence.clear_pause_request(home)
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 15)
        after = get_session_admission(authority.db, admission_id=receipt.admission_id)
        assert after['status'] == 'terminal' and after['outcome'] == 'completed'
        assert after['generation'] > before['generation']
        assert after['admission_id'] == before['admission_id']
        assert get_session_checkpoint(authority.db, session_id=ref.session_id) is None
        assert not authority.update_parked
        assert effects == ['effect-once'] and len(provider_inputs) == 2
        assert turns == [(physical, 'do some tool work', None),
                         (physical, pause_note('do some tool work'), 'auto_continue')]
        stored = authority.db.get_messages(physical)
        assert [m['role'] for m in stored] == ['user', 'assistant', 'tool', 'user', 'assistant']
        notices = [m for m in stored if m.get('display_kind') == 'auto_continue']
        assert len(notices) == 1 and notices[0]['content'] == pause_note('do some tool work')
        assert stored[-1]['content'] == 'done'
        assert sum(m.get('tool_call_id') == 'call_1' for m in stored) == 1
        assert provider_inputs[1][-1]['content'] == pause_note('do some tool work')
        assert any(m.get('role') == 'tool' and m.get('tool_call_id') == 'call_1'
                   for m in provider_inputs[1])
        assert all('display_kind' not in m for m in provider_inputs[1])
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM session_admissions').fetchone()[0] == 1
    finally:
        release.set()
        admission_fence.clear_hold(home)
        admission_fence.clear_pause_request(home)
        tasks = [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.close()
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['diagnostic', 'heartbeat'])
async def test_native_automation_real_agent_retains_quiet_traits_across_pause(tmp_path, monkeypatch, kind):
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_cli import admission_fence
    from hermes_constants import get_process_hermes_home
    from hermes_state_runtime import get_session_admission
    from hermes_state_update_pause import get_session_checkpoint
    from tests.gateway.test_shared_busy_receive_loop import Adapter
    from tests.waiting import async_wait_until
    from tui_gateway.update_pause import pause_note

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    monkeypatch.setattr('gateway.warning_notifications.warning_notifications_enabled',
                        lambda *a, **k: False)
    home = get_process_hermes_home()
    runner = GatewayRunner(GatewayConfig())
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True, home_channel=HomeChannel(platform=Platform.TELEGRAM, chat_id='composed-auto', name='Fixture'))
    adapter = Adapter()
    adapter.gateway_runner, runner.adapters = runner, {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='composed-' + kind)
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='composed-auto', chat_type='dm', user_id='fixture-user')
    entry = runner.session_store.get_or_create_session(source)
    physical = entry.session_id
    event = MessageEvent(text='do some tool work', source=source, internal=True, message_id='automation-original',
                         metadata={'gateway_session_key': entry.session_key, 'gateway_session_id': physical,
                                   **({'notification_category': 'diagnostic'} if kind == 'diagnostic' else {})})
    if kind == 'heartbeat':
        event._heartbeat_session_id = physical
    started, release = threading.Event(), threading.Event()
    effects, provider_inputs, turns = [], [], []
    agent = _make_agent([])
    del agent._execute_tool_calls
    agent.session_id, agent._session_db = physical, authority.db
    agent._save_trajectory = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None

    # Automation is only valid after a real native admission committed its route.
    warming = [True]

    def provider(**kwargs):
        provider_inputs.append(copy.deepcopy(kwargs['messages']))
        if not warming[0] and len(provider_inputs) == 1:
            return _tool_response(1)
        response = _final_response()
        if not warming[0] and kind == 'heartbeat':
            response.choices[0].message.content = 'NO_REPLY'
        return response

    agent.client.chat.completions.create.side_effect = provider

    def tool(name, args, *a, **k):
        started.set()
        assert release.wait(10)
        effects.append('effect-once')
        return 'ok'

    def allocate(turn, *a, **k):
        ctx = turn._ctx
        turns.append((ctx.session_id, ctx.message, ctx.persist_user_display_kind,
                      ctx.mute_notification_reply, ctx.scheduled_heartbeat))
        ctx.agent_holder[0] = agent
        return agent, len(turns) > 1

    monkeypatch.setattr(TurnRunner, '_resolve_turn_agent', allocate)
    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', lambda **k: ('test/model', {}))
    monkeypatch.setattr('model_tools.handle_function_call', tool)
    try:
        await adapter.handle_message(MessageEvent(text='establish native route', source=source, message_id='warmup'))
        await async_wait_until(lambda: bool(adapter.sent), timeout=15)
        assert adapter.sent == ['done']
        await async_wait_until(lambda: all(live.task.done() for live in authority.sessions.values()), timeout=15)
        warming[0] = False
        turns.clear()
        provider_inputs.clear()
        adapter.sent.clear()
        row = await authority.admit_automation(adapter, event, 'composed-' + kind)
        sid, admission_id = row.ref.session_id, row.admission_id
        await async_wait_until(started.is_set, timeout=15)
        admission_fence.set_hold(home)
        admission_fence.request_pause(home, expires_at=time.time() + 60)
        assert not authority.update_parked and effects == []
        release.set()
        await async_wait_until(lambda: bool(authority.update_parked), timeout=15)
        checkpoint = get_session_checkpoint(authority.db, session_id=sid)
        assert checkpoint and checkpoint['admission_id'] == admission_id
        before = get_session_admission(authority.db, admission_id=admission_id)
        assert before['status'] == 'started' and before['outcome'] is None
        assert len(provider_inputs) == 1 and effects == ['effect-once'] and not adapter.sent
        admission_fence.clear_hold(home)
        admission_fence.clear_pause_request(home)
        await asyncio.wait_for(authority.sessions[sid].task, 15)
        after = get_session_admission(authority.db, admission_id=admission_id)
        assert after['outcome'] == 'completed' and after['generation'] > before['generation']
        assert not authority.update_parked and get_session_checkpoint(authority.db, session_id=sid) is None
        assert effects == ['effect-once'] and len(provider_inputs) == 2
        expected_mute, expected_heartbeat = kind == 'diagnostic', kind == 'heartbeat'
        assert turns == [(physical, 'do some tool work', 'internal_notification', expected_mute, expected_heartbeat),
                         (physical, pause_note('do some tool work'), 'auto_continue', expected_mute, expected_heartbeat)]
        stored = authority.db.get_messages(physical)
        assert sum(m.get('tool_call_id') == 'call_1' for m in stored) == 1
        # A muted diagnostic turn persists every new row hidden (including the notice); heartbeat keeps the notice.
        assert sum(m.get('display_kind') == 'auto_continue' for m in stored) == (0 if kind == 'diagnostic' else 1)
        if kind == 'diagnostic':
            assert any(m.get('display_kind') == 'hidden' and pause_note('do some tool work') in str(m.get('content'))
                       for m in stored)
        assert not adapter.sent, 'quiet automation leaked an outbound response'
    finally:
        release.set()
        admission_fence.clear_hold(home)
        admission_fence.clear_pause_request(home)
        tasks = [*(live.task for live in authority.sessions.values() if live.task), *adapter._background_tasks]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        authority.db.close()
