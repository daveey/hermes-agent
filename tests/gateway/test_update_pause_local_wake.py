"""A producer wake on a local route (a Kanban notice, a /loop tick) parks for a core update.

The wake rides ``local_automation_v1`` (``admit_route_wake``). Before it bound an eligible pause scope,
such a turn never parked: a restart-safe drain waited on it for the whole claim lease. It parks at a
completed-tool step boundary with a durable checkpoint and resumes as the same admission once the
update hold lifts, re-authorized against its route. A noted completion (an async delegation or
process result) stays ineligible: with no viewer at resume it would be written as a note, not resumed.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.session_results import execution_result


def _row(payload, request_id='r'):
    return {'payload': payload, 'request_id': request_id}


def _live(platform):
    return SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value=platform)))


def _wake(producer):
    return {'text': 'wake', 'local_automation_v1': {
        'identity': json.dumps([producer, 'route', 'x'], separators=(',', ':')), 'owner': 's'}}


@pytest.mark.parametrize('payload,platform,eligible', [
    (_wake('kanban-wake'), 'local', True),
    (_wake('loop-tick'), 'local', True),
    (_wake('plugin'), 'local', True),
    (_wake('async_delegation'), 'local', False),  # noted completion: a note at an unviewed resume
    (_wake('completion'), 'local', False),
    (_wake('watch_match'), 'local', False),
    (_wake('cron'), 'local', False),
    (_wake('kanban-wake'), 'telegram', False),  # a detached route's wake: not checkpointed here
    ({**_wake('kanban-wake'), 'finite': True}, 'telegram', False),
    ({'text': 'hi', 'api_turn_v1': {}}, 'local', False),
    ({'text': 'hi'}, 'local', True),
    ({'text': 'hi', 'native_text_v1': {'automation': {'identity': 'x'}}}, 'telegram', True),
    ({'text': 'hi'}, 'telegram', False),
], ids=['kanban', 'loop', 'plugin', 'delegation', 'completion', 'watch', 'cron', 'detached', 'finite',
        'api', 'local-chat', 'native-notice', 'unenveloped'])
def test_pause_eligibility_by_admission_kind(payload, platform, eligible):
    from gateway.session_update_pause_ingress import supported
    assert supported(None, _row(payload), None, _live(platform), is_api=False) is eligible


def test_api_turn_is_never_eligible():
    from gateway.session_update_pause_ingress import supported
    assert supported(None, _row(_wake('kanban-wake')), None, _live('local'), is_api=True) is False


@pytest.mark.asyncio
async def test_local_kanban_wake_parks_at_boundary_then_resumes_same_admission(tmp_path, monkeypatch):
    from agent.turn_iteration_prep import _step_boundary_pause
    from gateway import session_update_pause
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.response_filters import display_kind_for_event
    from gateway.session_authority import initialize_session_authority
    from gateway.session_automation import admit_route_wake
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from gateway.session_update_pause_hook import installed, update_pausable
    from hermes_cli import admission_fence
    from hermes_state import SessionDB
    from hermes_state_runtime import get_session_admission
    from tests.waiting import async_wait_until
    from tui_gateway.update_pause import PAUSE_REASON, pause_note

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='wake-pause', db=db)
    held = [True]
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: held[0])
    monkeypatch.setattr(admission_fence, 'pause_requested', lambda root: held[0])
    seen, verdicts = [], []

    async def turn(event, source, quick_key, run_generation):
        seen.append(event)
        if len(seen) == 1:
            sid = event.metadata['gateway_session_id']
            history = [
                {'role': 'user', 'content': event.text},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'k1', 'type': 'function', 'function': {'name': 'kanban_show', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'card', 'tool_call_id': 'k1', 'tool_name': 'kanban_show'}]
            authority.db.append_messages_batch(sid, history)
            agent = SimpleNamespace()
            # TurnRunner's exact install call: the hook exists only for an eligible bound scope.
            with installed(agent, message_id=event.message_id) as hooked:
                verdicts.append((hooked, update_pausable(agent), _step_boundary_pause(agent, history, 1)))
            assert not update_pausable(agent)  # restored after the run
            execution_result.get()['result'] = {'paused_for_update': True, 'interrupted': True,
                                                'final_response': '', 'messages': history}
            return None
        assert event.internal is False and not event.allow_gateway_control
        assert event.text == pause_note('kanban: task t_1 blocked')
        assert display_kind_for_event(event) == 'auto_continue'
        assert event.message_id.startswith('update-resume:')
        execution_result.get()['result'] = {'final_response': 'continued', 'messages': []}
        return 'continued'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        viewer = SessionActor('test-owner', 'default', frozenset(
            {'session:create', 'session:submit', 'session:read'}), 'viewer')
        ref = create_local_session(authority, viewer, dict(request_id='cli', source='tui', model='fixture',
                                                          cwd=str(tmp_path), toolsets=[]))
        await authority.attach(viewer, ref, history=False)
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)
        assert await admit_route_wake(runner, adapter, live.source, 'kanban: task t_1 blocked', 'kanban-wake') is True
        await async_wait_until(lambda: bool(authority.update_parked), timeout=10, what='the wake parked')
        assert verdicts == [(True, True, PAUSE_REASON)]
        assert len(seen) == 1
        parked = authority.update_parked[ref.session_id]
        assert get_session_admission(db, admission_id=parked['admission_id'])['status'] == 'started'
        held[0] = False
        authority._schedule(ref)
        await async_wait_until(lambda: len(seen) == 2, timeout=10, what='the wake resumed')
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 10)
        row = get_session_admission(db, admission_id=parked['admission_id'])
        assert (row['status'], row['outcome']) == ('terminal', 'completed')
    finally:
        held[0] = False
        tasks = [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()
