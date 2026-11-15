"""Authenticated envelope -> real CLI/registry/tool -> isolated intake database."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_origin_owner as courier
from tests.hermes_cli import test_kanban_selfwork_entry as entry
from tools.registry import registry

world = courier.world


@pytest.fixture
def request_envelope(world, tmp_path):
    (tmp_path / 'config.yaml').write_text(json.dumps({'kanban': {'self_work': {
        'home': {'gateway': 'owner', 'profile': 'default', 'board': 'default'}}}}), encoding='utf-8')
    world['action'] = 'selfwork-create'
    world['args'].pop('origin')
    world['args']['create'].pop('session_id')
    world['args']['create'].update(self_work=entry.support.declaration(), requires=[])
    return world


def invoke(request, monkeypatch, capsys):
    return courier.invoke(request, monkeypatch, capsys, command='selfwork-create')


def test_nochat_create_append_replay_without_ambient_inheritance(request_envelope, monkeypatch, capsys):
    monkeypatch.setenv('HERMES_TENANT', 'ambient-tenant')
    monkeypatch.setenv('HERMES_SESSION_ID', 'ambient-session')
    monkeypatch.setenv('HERMES_ORIGIN_SESSION', 'ambient-origin')
    first = invoke(request_envelope, monkeypatch, capsys)
    assert first.get('created') is True, first
    assert first['handoff_complete'] is True
    assert first['subscribed'] is False
    request_envelope['id'] = 'request-owner-002'
    request_envelope['args']['create']['title'] = 'Additional evidence'
    second = invoke(request_envelope, monkeypatch, capsys)
    assert second.get('disposition') == 'append', second
    assert second['task_id'] == first['task_id']
    assert invoke(request_envelope, monkeypatch, capsys)['replayed'] is True
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, first['task_id'])
        assert task.session_id is None
        assert task.tenant != 'ambient-tenant'
        assert task.created_by == 'gw:origin'
        assert len(kb.list_tasks(conn)) == 1
        assert len(kb.list_comments(conn, task.id)) == 1
        assert conn.execute('SELECT COUNT(*) FROM kanban_notify_subs').fetchone()[0] == 0
        # Origin and sync tables are lazy; absence also proves no rows.
        for table in ('origin_creation_intents', 'task_chat_links'):
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('field,value', [
    ('from', None), ('from', 'gw:'), ('from', 'gw:bad/sender'), ('from', 'user:origin'),
    ('to_gw', 'foreign'), ('coll', 'sessions'), ('key', 'other/default'),
    ('key', 'default/default/extra'), ('key', 'default/missing'), ('action', 'create'),
])
def test_wrong_route_rejected(request_envelope, monkeypatch, capsys, field, value):
    request_envelope[field] = value
    result = invoke(request_envelope, monkeypatch, capsys)
    assert result['status'] == 'rejected', result
    with kbc.connect_closing() as conn:
        assert not kb.list_tasks(conn)


@pytest.mark.parametrize('entry_path', ['cli', 'tool'])
def test_postcommit_selfwork_error_stays_pending_then_replays(request_envelope, monkeypatch, capsys, entry_path):
    from hermes_cli import kanban_selfwork as policy, kanban_selfwork_owner as owner
    from hermes_cli.kanban_selfwork_receiver import receive
    render = owner._render
    def unavailable(*args, **kwargs):
        raise policy.SelfWorkError('response unavailable after committed intake')
    monkeypatch.setattr(owner, '_render', unavailable)
    call = lambda: (invoke(request_envelope, monkeypatch, capsys) if entry_path == 'cli'
                    else receive(request_envelope))
    result = call()
    assert result['status'] == 'pending', result
    with kbc.connect_closing() as conn:
        assert len(kb.list_tasks(conn)) == 1
    monkeypatch.setattr(owner, '_render', render)
    replay = call()
    assert replay['replayed'] is True
    assert replay['handoff_complete'] is True
    with kbc.connect_closing() as conn:
        assert len(kb.list_tasks(conn)) == 1


def test_public_context_is_not_authentication(request_envelope):
    args = {**request_envelope['args']['create'], 'board': 'default',
            'selfwork_intent': {'request_id': 'forged', 'sender': 'gw:origin',
                                'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default'}}}
    result = json.loads(registry.dispatch('kanban_create', args))
    assert not result.get('created'), result
    with kbc.connect_closing() as conn:
        assert not kb.list_tasks(conn)


def test_optional_origin_retains_sender_equality(request_envelope, monkeypatch, capsys):
    request_envelope['args']['origin'] = {'gateway': 'foreign', 'profile': 'work', 'session': 'chat'}
    request_envelope['args']['create']['session_id'] = 'creator'
    assert invoke(request_envelope, monkeypatch, capsys)['status'] == 'rejected'
    request_envelope['args']['origin']['gateway'] = 'origin'
    result = invoke(request_envelope, monkeypatch, capsys)
    assert result.get('created') is True, result
    assert result['code'] == 'origin_bind_pending'
    assert result['handoff_complete'] is False


@pytest.mark.parametrize('field,value', [('session_id', 'fake-chat'), ('origin_session', 'fake-origin'),
                                       ('selfwork_intent', {}), ('board', 'other'), ('self_work', None)])
def test_invalid_create_refused(request_envelope, monkeypatch, capsys, field, value):
    request_envelope['args']['create'][field] = value
    assert invoke(request_envelope, monkeypatch, capsys)['status'] == 'rejected'
    with kbc.connect_closing() as conn:
        assert not kb.list_tasks(conn)


def test_replay_binds_sender_and_revalidates_home(request_envelope, tmp_path, monkeypatch, capsys):
    first = invoke(request_envelope, monkeypatch, capsys)
    assert first.get('created') is True, first
    assert invoke(request_envelope, monkeypatch, capsys)['replayed'] is True
    request_envelope['from'] = 'gw:different'
    conflict = invoke(request_envelope, monkeypatch, capsys)
    assert not conflict.get('replayed'), conflict
    request_envelope['from'] = 'gw:origin'
    (tmp_path / 'config.yaml').write_text('{}', encoding='utf-8')
    refused = invoke(request_envelope, monkeypatch, capsys)
    assert not refused.get('replayed'), refused
    with kbc.connect_closing() as conn:
        assert len(kb.list_tasks(conn)) == 1
