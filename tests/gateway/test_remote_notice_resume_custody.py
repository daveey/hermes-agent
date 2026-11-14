"""A completed resumed batch owns its exact remote events without an original keyed row."""
import asyncio
import json

import pytest

from gateway import kanban_remote_owner as owner, session_update_pause
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_automation import admit_route_wake
from gateway.session_contract import SessionActor
from gateway.session_local import create_local_session
from gateway.session_results import admission_result, execution_result
from hermes_state import SessionDB
from hermes_state_runtime import get_session_admission
from tests.gateway import test_kanban_remote_owner_delivery as remote
from tests.waiting import async_wait_until
from tui_gateway import kanban_remote_notices as notices


@pytest.mark.asyncio
@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('result_mode', ['completed', 'failed', 'interrupted', 'unpersisted', 'missing'])
async def test_completed_resumed_batch_not_reoffered(tmp_path, monkeypatch, compact, restart, result_mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    hub = tmp_path / 'hub'
    hub.mkdir()
    monkeypatch.setenv('HERMES_HUB_STATE', str(hub))
    notices._reset_for_tests()
    owner._reset_for_tests()
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='custody', db=db)
    actor = SessionActor('test-owner', 'default', frozenset(
        {'session:create', 'session:submit', 'session:read'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='local', source='tui', model='fixture',
                                                     cwd=str(tmp_path), toolsets=[]))
    await authority.attach(actor, ref, history=False)
    live = authority.sessions[ref.session_id]
    adapter = runner._adapter_for_source(live.source)
    if live.task:
        await asyncio.wait_for(live.task, 10)
    physical = ref.session_id
    cards = [remote._done(f't_{i:016x}', 100 + i, chat=ref.session_id) for i in range(4)]
    remote._publish(hub, cards)
    claims = notices.collect(ref.session_id, 'default', per_link=True)
    assert len(claims) == 4
    keys = {claim['event_key'] for claim in claims}
    held, seen = [True], []
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: held[0])

    async def turn(event, source, quick_key, run_generation):
        seen.append(event)
        if len(seen) == 1:
            assert set(event.metadata['kanban_event_keys']) == keys
            # Deliberately no original keyed row: the verified incident's missing evidence.
            db.append_messages_batch(physical, [
                {'role': 'user', 'content': event.text},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'k', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'done', 'tool_call_id': 'k', 'tool_name': 'example'}])
            execution_result.get()['result'] = {'paused_for_update': True}
            return None
        assert event.internal is False and not event.allow_gateway_control
        assert 'kanban_event_keys' not in event.metadata
        db.append_message(physical, 'user', event.text, display_kind='auto_continue')
        db.append_message(physical, 'assistant', 'all five handled')
        if compact:
            db._execute_write(lambda conn: conn.execute(
                'UPDATE messages SET active=0, compacted=1 WHERE session_id=?', (physical,)))
            db.append_message(physical, 'assistant', 'compacted summary')
        result = {'final_response': 'all five handled', 'messages': [], 'completed': True,
                  'agent_persisted': result_mode != 'unpersisted', 'compacted_in_place': compact}
        if result_mode in ('failed', 'interrupted'):
            result[result_mode] = True
        if result_mode != 'missing':
            execution_result.get()['result'] = result
        return 'all five handled'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        # Hold scheduling, not admission: all five real ingress calls fold at the FIFO tail.
        schedule = authority._schedule_admitted
        with monkeypatch.context() as gated:
            gated.setattr(authority, '_schedule', lambda *args: None)
            assert await admit_route_wake(runner, adapter, live.source, 'local card', 'kanban-wake',
                {'kanban_cards': [
                    {'task_id': 't_0000000000000005', 'board': 'default', 'kinds': [], 'text': 'local card'}]})
            for claim in claims:
                assert await owner._admit(runner, adapter, live, frozenset({ref.session_id, physical}), physical, claim)
        schedule(ref)
        await async_wait_until(lambda: bool(authority.update_parked), timeout=10)
        parked = authority.update_parked[ref.session_id]
        row = get_session_admission(db, admission_id=parked['admission_id'])
        assert len(row['payload']['local_automation_v1']['kanban_cards']) == 5
        if restart:
            authority.retiring = True
            await asyncio.wait_for(authority.sessions[ref.session_id].task, 10)
            old_epoch = authority.epoch
            runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
            runner.session_store._db = runner._session_db = db
            monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
            authority = await initialize_session_authority(
                runner, profile_id='default', instance_id='replacement', db=db)
            assert authority.epoch > old_epoch
            await authority.attach(actor, ref, history=False)
        held[0] = False
        authority._schedule(ref)
        await async_wait_until(lambda: len(seen) == 2, timeout=10)
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 10)
        row = get_session_admission(db, admission_id=parked['admission_id'])
        expected = {'missing': 'failed', 'failed': 'failed', 'interrupted': 'interrupted'}.get(
            result_mode, 'completed')
        assert (row['status'], row['outcome']) == ('terminal', expected)
        assert admission_result(db, row['admission_id']) is not None
        assert not db.kanban_notice_keys_delivered(ref.session_id, keys)
        if restart:
            remote._restart(remote._dead_pid())
        else:
            owner._reconcile(db)
        # A new remote owner has no in-memory custody; use the production collect callback.
        owner._reset_for_tests()
        def has(key, tid):
            return notices.notice_in_transcript(db, physical, key, tid)
        offered = notices.collect(ref.session_id, 'default', per_link=True, transcript_has=has)
        if result_mode != 'completed':
            assert {claim['event_key'] for claim in offered} == keys
            assert all(not has(key, '') for key in keys)
            return
        assert offered == []
        assert notices.collect(ref.session_id, 'default', per_link=True) == []
        assert all(has(key, '') for key in keys)
        # A new event for an already handled task must still be offered.
        later = remote._done('t_0000000000000000', 999, chat=ref.session_id)
        later['completed_at'] += 10  # a distinct terminal transition, not the ledger's same-second state
        later['completion_notice']['created_at'] += 10
        remote._publish(hub, [later])
        offered = notices.collect(ref.session_id, 'default', per_link=True, transcript_has=has)
        assert [claim['event_key'] for claim in offered] == [remote._key('t_0000000000000000', 999)]
    finally:
        held[0] = False
        tasks = [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()
        notices._reset_for_tests()
        owner._reset_for_tests()
