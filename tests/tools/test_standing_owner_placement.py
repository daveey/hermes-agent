"""Standing owner discovery is fallback routing, never forwarding custody."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.tools import test_kanban_cross_board_creator_chats as helpers
from tests.tools.test_kanban_worker_created_chats import WORKER
from tools import kanban_tools as tools

home = helpers.home
placed = helpers.placed


def test_real_owner_is_not_displaced_by_replica(placed, monkeypatch):
    from hermes_cli import kanban_creator_chats as chats
    from tests.tools.test_kanban_worker_created_chats import CHAT

    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='Actual owner', session_id=WORKER, settle_owner_chat=False)
        helpers._link(conn, creator, 'here', None, CHAT)
    monkeypatch.setattr(chats, '_replica_chats', lambda *a: [
        {'chat_gateway': 'origin', 'chat_profile': 'default', 'chat_session': helpers.FOREIGN_CHAT}])
    helpers._place(creator, monkeypatch)
    assert placed == [{'target': 'hub', 'origin_session': CHAT, 'origin_profile': 'default'}]


def test_empty_local_discovery_preserves_replica_standing_origin(home, monkeypatch):
    from hermes_cli import kanban_creator_chats as chats
    from tools.kanban_tools_placement import _placement_origin

    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='No local links', session_id=WORKER, settle_owner_chat=False)
        monkeypatch.setattr(chats, '_replica_chats', lambda *a: [
            {'chat_gateway': 'origin', 'chat_profile': 'default', 'chat_session': 'kanban_owner_pressure'}])
        assert _placement_origin(conn, creator, WORKER) == {
            'session_id': 'kanban_owner_pressure', 'origin_profile': 'default', 'origin_gateway': 'origin'}


@pytest.mark.parametrize('replica_kind', ['real', 'standing', 'absent'])
def test_standing_owner_placement_uses_real_replica_before_forward(home, monkeypatch, replica_kind):
    from hermes_cli import kanban_creator_chats as chats, kanban_placement as kp
    from hermes_cli.kanban_origin_forward import authorize
    from hermes_cli import kanban_owner_chat as owner
    from hermes_state import SessionDB

    sid = owner.session_id('worker pressure')
    with SessionDB(home / 'state.db') as state:
        state.create_session(sid, source='desktop')
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='Picked up creator', session_id=WORKER, settle_owner_chat=False)
        helpers._link(conn, creator, 'stale-worker', 'default', sid)
    chosen = helpers.FOREIGN_CHAT if replica_kind == 'real' else owner.session_id('projected')
    replica = [] if replica_kind == 'absent' else [
        {'chat_gateway': 'origin', 'chat_profile': 'default', 'chat_session': chosen}]
    monkeypatch.setattr(chats, '_replica_chats', lambda *a: replica)
    monkeypatch.setattr(kp, 'decide', lambda *a, **k: {'where': 'hub', 'why': 'test'})
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    monkeypatch.setenv('HERMES_SESSION_ID', WORKER)
    monkeypatch.setenv('HERMES_SESSION_KEY', WORKER)
    from hermes_cli import kanban_placement_provenance as provenance
    monkeypatch.setattr(provenance, 'forwarding_authority', lambda *a, **k: {
        'creator': {'gateway': 'origin', 'profile': 'default', 'board': 'default', 'task_id': creator},
        'run_uid': 'run-1'})
    sent = []

    def forward(args, *, target, origin_session, origin_profile, self_gw, origin_gateway, creator, run_uid, **kw):
        sent.append((origin_gateway, origin_profile, origin_session))
        binding = {'binding_id': 'native-' + creator['task_id'], 'version': 1, 'publication_version': 1,
                   'origin': {'gateway': 'origin', 'profile': 'default', 'session': helpers.FOREIGN_CHAT},
                   'owner': creator, 'task_record_key': 'default/default/' + creator['task_id']}
        try:
            authorize(ledger=home / 'absent-ledger.db', origin={
                'gateway': origin_gateway, 'profile': origin_profile, 'session': origin_session},
                creator=creator, gateway=origin_gateway, profile='default', sender='gw:here', run_uid=run_uid,
                lease={'task_id': creator['task_id'], 'holder': 'here', 'run_uid': run_uid, 'expires': 2000},
                now_ms=1000, session_exists=lambda _: True,
                native_lookup=lambda o, c: binding if o == binding['origin'] else None)
        except ValueError as exc:
            return {'ok': False, 'fatal': True, 'error': str(exc)}
        return {'ok': True, 'request_id': 'placement:here:regression', 'queued_only': True}

    monkeypatch.setattr(kp, 'create_remote', forward)
    for _ in range(2):
        out = json.loads(tools._handle_create({'title': 'Child', 'assignee': 'default', 'requires': []}))
        if replica_kind == 'real':
            assert out.get('ok') is True, out
        else:
            assert out.get('created') is False, out
            assert 'origin_forward_binding_missing' in out['error']
    expected = ('origin', 'default', helpers.FOREIGN_CHAT) if replica_kind == 'real' else ('stale-worker', 'default', sid)
    assert sent == [expected, expected]
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert not (home / 'absent-ledger.db').exists()
