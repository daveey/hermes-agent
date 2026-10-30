"""Frozen user-steps decisions follow compression, never a new conversation."""
import json

import pytest

from tests.gateway.test_local_resume_compression_child import _runner, _compress


@pytest.mark.asyncio
@pytest.mark.parametrize('rotation', ['compression', 'reset', 'compressed-reset'])
@pytest.mark.parametrize('generic_identity', ['generic-notice', '["user-steps-other","operation"]',
                                              '["user-steps","operation","extra"]'])
async def test_queued_frozen_decision_target_fence(tmp_path, monkeypatch, rotation, generic_identity):
    from gateway import run
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_automation import reset_and_rehome_messages, restore_local_automation
    from gateway.platforms.event import MessageEvent
    from hermes_state_runtime import get_session_admission, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    runner = _runner(tmp_path)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')
    monkeypatch.setattr(authority, '_schedule_admitted', lambda *args: None)
    actor = Principal('human', 'fixture', frozenset({'session:create', 'session:read'}), 'socket')
    ref = create_local_session(authority, actor, dict(request_id='create', source='gui',
        cwd=str(tmp_path), model='m', toolsets=[]))
    live = authority.sessions[ref.session_id]
    store = runner.session_store
    try:
        ids = []
        for identity, text in [(json.dumps(['user-steps', 'operation'], separators=(',', ':')), 'frozen answer'),
                               (generic_identity, 'generic automation')]:
            event = MessageEvent(text=text, source=live.source, internal=True, message_id=identity,
                metadata={'gateway_session_key': live.route, 'gateway_session_id': ref.session_id})
            receipt = await authority.admit_automation(runner._adapter_for_source(live.source), event, identity)
            ids.append(receipt.admission_id)
        before = [get_session_admission(authority.db, admission_id=i) for i in ids]
        with authority.db._read_ctx() as conn:
            digest = conn.execute('SELECT payload_digest FROM session_admissions WHERE admission_id=?', (ids[0],)).fetchone()[0]
        import asyncio
        waiter = authority.waiters[ids[0]] = asyncio.get_running_loop().create_future()
        monkeypatch.setattr(authority, '_schedule', lambda *args: None)
        events = []
        publish = live.event_stream.publish
        def record_event(sid, event, **kwargs):
            events.append(event)
            return publish(sid, event, **kwargs)
        monkeypatch.setattr(live.event_stream, 'publish', record_event)
        target = ref.session_id
        if rotation != 'reset':
            store._record_gateway_session_peer(ref.session_id, live.route, live.source)
            _compress(authority.db, ref.session_id, 'compressed-child', 'local')
            store._record_gateway_session_peer('compressed-child', live.route, live.source)
            assert store.advance_compression_session(live.route, ref.session_id, 'compressed-child')
            target = 'compressed-child'
        if rotation != 'compression':
            old = authority.db.get_compression_tip(ref.session_id)
            entry, release = await reset_and_rehome_messages(runner, live.route, old)
            release()
            target = entry.session_id
            assert target != ref.session_id
        rows = [get_session_admission(authority.db, admission_id=i) for i in ids]
        if rotation != 'compression':
            assert rows[0]['status'] == 'terminal'
            assert rows[0]['outcome'] == 'cancelled'
            assert rows[0]['payload'] == before[0]['payload']
            with authority.db._read_ctx() as conn:
                assert conn.execute('SELECT payload_digest FROM session_admissions WHERE admission_id=?', (ids[0],)).fetchone()[0] == digest
            assert waiter.done() and waiter.result() is None
            assert [e['admission_id'] for e in events if e.get('outcome') == 'cancelled'] == [ids[0]]
            expected = ids[1]
        else:
            assert restore_local_automation(authority, ref, rows[0]).metadata['gateway_session_id'] == target
            expected = ids[0]
        # Generic reset semantics and FIFO remain intact; no new-conversation decision can be claimed.
        generic = restore_local_automation(authority, ref, rows[1])
        assert generic.text == 'generic automation'
        assert generic.metadata['gateway_session_id'] == target
        claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)
        assert claimed['admission_id'] == expected
        for old, row in zip(before, rows):
            assert (row['admission_id'], row['request_id'], row['seq']) == (
                old['admission_id'], old['request_id'], old['seq'])
    finally:
        store.close_all_db_handles()
