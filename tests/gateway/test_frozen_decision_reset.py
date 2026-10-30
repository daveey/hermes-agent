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


@pytest.mark.asyncio
async def test_caller_cancelled_during_reset_write_still_settles_frozen_decision(tmp_path, monkeypatch):
    """The reset caller may be cancelled while the ordered DB write is suspended; the shielded
    tracked write still commits, cancels the frozen decision once, and runs nothing in the fresh
    conversation. Barriers are events, never sleeps."""
    import asyncio
    import threading

    import hermes_state_runtime
    from gateway import run
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority
    from gateway.session_automation import reset_and_rehome_messages, restore_local_automation
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_state_runtime import claim_session_input, get_session_admission

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    runner = _runner(tmp_path)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')
    monkeypatch.setattr(authority, '_schedule_admitted', lambda *args: None)
    monkeypatch.setattr(authority, '_schedule', lambda *args: None)
    actor = Principal('human', 'fixture', frozenset({'session:create', 'session:read'}), 'socket')
    ref = create_local_session(authority, actor, dict(request_id='create', source='gui',
        cwd=str(tmp_path), model='m', toolsets=[]))
    live = authority.sessions[ref.session_id]
    store = runner.session_store
    entered, release, calls = threading.Event(), threading.Event(), []
    try:
        ids = []
        for identity, text in [(json.dumps(['user-steps', 'operation'], separators=(',', ':')), 'frozen answer'),
                               ('generic-notice', 'generic automation')]:
            event = MessageEvent(text=text, source=live.source, internal=True, message_id=identity,
                metadata={'gateway_session_key': live.route, 'gateway_session_id': ref.session_id})
            ids.append((await authority.admit_automation(runner._adapter_for_source(live.source), event, identity)).admission_id)
        before = [get_session_admission(authority.db, admission_id=i) for i in ids]
        with authority.db._read_ctx() as conn:
            digest = conn.execute('SELECT payload_digest FROM session_admissions WHERE admission_id=?', (ids[0],)).fetchone()[0]
        waiter = authority.waiters[ids[0]] = asyncio.get_running_loop().create_future()
        events = []
        publish = live.event_stream.publish

        def record_event(sid, event, **kwargs):
            events.append(event)
            return publish(sid, event, **kwargs)
        monkeypatch.setattr(live.event_stream, 'publish', record_event)

        real = hermes_state_runtime.retarget_local_automation

        def suspended(*args, **kwargs):
            calls.append(1)
            entered.set()
            assert release.wait(30)
            return real(*args, **kwargs)
        monkeypatch.setattr(hermes_state_runtime, 'retarget_local_automation', suspended)

        old = authority.db.get_compression_tip(ref.session_id)
        caller = asyncio.ensure_future(reset_and_rehome_messages(runner, live.route, old))
        assert await asyncio.to_thread(entered.wait, 30)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # The write is still suspended: the cancelled caller settled nothing itself, and the owner's
        # mutation lock (the drain's claim fence) stays held until the commit lands.
        assert len(mutation_tasks(authority)) == 1 and not waiter.done() and events == []
        assert live.mutation_lock.locked()
        release.set()
        await asyncio.gather(*mutation_tasks(authority))
        await asyncio.wait_for(live.mutation_lock.acquire(), 30)  # released once the commit landed
        live.mutation_lock.release()

        assert len(calls) == 1 and not mutation_tasks(authority)
        fresh = store.lookup_by_session_key(live.route).session_id
        assert fresh != ref.session_id
        rows = [get_session_admission(authority.db, admission_id=i) for i in ids]
        assert (rows[0]['status'], rows[0]['outcome']) == ('terminal', 'cancelled')
        assert rows[0]['payload'] == before[0]['payload']
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT payload_digest FROM session_admissions WHERE admission_id=?', (ids[0],)).fetchone()[0] == digest
        assert waiter.done() and waiter.result() is None
        assert [e['admission_id'] for e in events if e.get('outcome') == 'cancelled'] == [ids[0]]
        assert rows[1]['status'] == 'queued'
        generic = restore_local_automation(authority, ref, rows[1])
        assert generic.metadata['gateway_session_id'] == fresh
        for prior, row in zip(before, rows):
            assert (row['admission_id'], row['request_id'], row['seq']) == (
                prior['admission_id'], prior['request_id'], prior['seq'])
        # The decision never reaches the fresh conversation: only the generic row is claimable.
        assert claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)['admission_id'] == ids[1]
    finally:
        release.set()  # never leave the write thread parked if an assertion failed first
        store.close_all_db_handles()
