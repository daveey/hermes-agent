import json
import sqlite3
import pytest
from gateway import kanban_remote_owner as kro, session_automation, kanban_wake_fold
from tests.gateway.test_kanban_wake_fold import _local_chat, _automation


@pytest.mark.parametrize('envelope', ['local_automation_v1', 'native_text_v1'])
@pytest.mark.parametrize('extra', ['', 'x' * 20000])
def test_refreshed_display_matches_bounded_model_text(monkeypatch, envelope, extra):
    tid = 't_remote'
    text = 'Blocked\nNext action for the user: choose\nContext' + extra
    descriptor = {'identity': json.dumps(['kanban-wake', 'fixture']),
                  'display_kind': 'kanban_notice', 'display_text': text,
                  'kanban_cards': [{'task_id': tid, 'board': 'default',
                                    'kinds': ['blocked'], 'text': text}]}
    payload = {'text': text, envelope: descriptor if envelope == 'local_automation_v1'
               else {'automation': descriptor}}
    monkeypatch.setattr(kanban_wake_fold, '_live_task', lambda *a, **kw: ('archived', ''))
    kanban_wake_fold.refresh_claimed(payload)
    limit = session_automation._DISPLAY_TEXT_LIMIT
    expected = payload['text'] if len(payload['text']) <= limit else payload['text'][:limit - 1] + '…'
    assert descriptor['display_text'] == expected
    assert 'Live status: archived' in expected
    assert 'Next action for the user:' not in expected
    assert session_automation.admission_display(payload)['display_text'] == expected

@pytest.mark.asyncio
async def test_standalone_archived_replica_display(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc, kanban_db as kb, kanban_replica_freshness as fresh
    from hermes_cli.kanban_block_notice import block_notice
    from gateway.session_ingress import execute_admission
    from gateway.session_contract import SessionRef
    from tests.gateway.session_claims import claim
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    tid = 't_01a1dbc467a3e3f5'
    with kbc.connect() as conn:
        from hermes_cli import kanban_db_sync as sync
        sync.apply_sync_schema_steps(conn)
        assert kb.get_task(conn, tid) is None
    replica = tmp_path / 'replica.db'
    with sqlite3.connect(replica) as conn:
        conn.execute('CREATE TABLE tasks(id TEXT, status TEXT, ready_since INTEGER, sync_version INTEGER, result TEXT)')
        conn.execute('CREATE TABLE task_runs(id INTEGER, task_id TEXT, summary TEXT, ended_at INTEGER, started_at INTEGER)')
        conn.execute('INSERT INTO tasks VALUES (?, ?, NULL, 12, ?)', (tid, 'archived', 'withdrawn'))
    monkeypatch.setattr(fresh, '_replica', lambda _: (replica, 0))
    text = block_notice({'status':'blocked','block_kind':'needs_input','title':'Fixture',
        'human_request': {'action':'Choose A; unblock to proceed; archive to drop', 'why_human':'decision',
                         'scope':'fixture','interaction':'reply','evidence':'fixture'}}, tid, 'default')
    item = {'ref':f'hub/default/default/{tid}', 'state':'blocked@1', 'event_key':'remote:fixture:1', 'text':text}
    metadata = kro._metadata(item)
    authority, adapter, source, queued, _ = await _local_chat(tmp_path, monkeypatch)
    seen = []
    async def turn(event):
        from gateway.response_filters import persisted_user_metadata_for_event, display_kind_for_event
        display = persisted_user_metadata_for_event(event)
        authority.db.append_message(source.chat_id, 'user', content=event.text,
                                    display_kind=display_kind_for_event(event), display_metadata=display)
        seen.append((event.text, display))
        return 'ok'
    monkeypatch.setattr(authority.runner, '_handle_message', turn)
    try:
        assert await session_automation.admit_route_wake(authority.runner, adapter, source, text, kro.PRODUCER, metadata)
        [row] = _automation(queued())
        descriptor = kanban_wake_fold._descriptor(row['payload'])
        ref = SessionRef(authority.profile_id, descriptor['owner'])
        from gateway.session_claim_publication import stamp_claim
        live = authority.sessions[ref.session_id]
        starts = []
        publish = live.event_stream.publish
        def capture(session_id, payload, **kwargs):
            if kwargs.get('event_type') == 'message.start':
                starts.append(payload)
            return publish(session_id, payload, **kwargs)
        monkeypatch.setattr(live.event_stream, 'publish', capture)
        claimed = claim(authority, ref, row)
        import threading
        loop_thread = threading.get_ident()
        refresh = kanban_wake_fold.refresh_claimed
        refresh_calls = []
        def observed_refresh(payload):
            assert threading.get_ident() != loop_thread
            assert authority.executing[ref.session_id] == claimed['generation']
            assert live.event_stream.execution['admission_id'] == claimed['admission_id']
            if not refresh_calls:
                assert starts == []
            refresh_calls.append(True)
            return refresh(payload)
        monkeypatch.setattr(kanban_wake_fold, 'refresh_claimed', observed_refresh)
        await stamp_claim(authority, ref, live, claimed)
        assert refresh_calls
        assert len(starts) == 1
        assert 'Next action for the user:' not in starts[0]['display_text']
        assert 'Live status: archived' in starts[0]['display_text']
        await execute_admission(authority, ref, claimed)
        assert len(seen) == 1
        model_text, meta = seen[0]
        assert 'Live status: archived' in model_text
        assert 'Next action for the user:' not in model_text
        rows = authority.db.get_messages(source.chat_id)
        stored = rows[-1]['display_metadata']
        stored = json.loads(stored) if isinstance(stored, str) else stored
        assert stored['display_text'] == meta['display_text']
        assert 'Next action for the user:' not in stored['display_text'], stored['display_text']
    finally:
        authority.db.close()
