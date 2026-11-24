"""Forwarded creator is private authenticated transport, never a public argument."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_origin_owner as support

world = support.world


@pytest.fixture
def attested(world, monkeypatch):
    from hermes_cli import kanban_origin_native as native
    world['from'] = 'gw:owner'
    world['args']['origin'] = {'gateway': 'owner', 'profile': 'default', 'session': 'original'}
    monkeypatch.setattr(native, '_primary_scope', lambda conn: ('owner', 'default', 'default'))
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title='Implementation', session_id='creator')
        native._install_revocation(conn)
        binding = {'binding_id': 'native-' + parent, 'version': 1, 'publication_version': 1,
                   'origin': world['args']['origin'],
                   'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default', 'task_id': parent},
                   'task_record_key': 'default/default/' + parent}
        conn.execute('INSERT INTO origin_native_receipts VALUES (?,?,NULL,1)', (parent, json.dumps(binding)))
        conn.commit()
    world['args']['creator'] = {'gateway': 'owner', 'profile': 'default',
                                'board': 'default', 'task_id': parent}
    world['args']['create']['parents'] = [parent]
    return world, parent


def test_forwarded_creator_stamped_and_replayed(attested, monkeypatch, capsys):
    world, parent = attested
    first = support.invoke(world, monkeypatch, capsys)
    assert first.get('created') is True, first
    with kbc.connect_closing() as conn:
        conn.execute('DELETE FROM origin_native_receipts WHERE task_id=?', (parent,))
        conn.commit()
    second = support.invoke(world, monkeypatch, capsys)
    assert second['task_id'] == first['task_id']
    with kbc.connect_closing() as conn:
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created'",
                              (first['task_id'],)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])['creator_task_id'] == parent


@pytest.mark.parametrize('damage', ['missing', 'origin', 'owner', 'primary', 'namespace'])
def test_unproven_native_creator_is_refused_atomically(attested, monkeypatch, capsys, damage):
    from hermes_cli import kanban_origin_native as native
    world, parent = attested
    if damage == 'primary':
        monkeypatch.setattr(native, '_primary_scope', lambda conn: None)
    elif damage == 'namespace':
        world['args']['creator']['board'] = 'other'
    else:
        with kbc.connect_closing() as conn:
            if damage == 'missing':
                conn.execute('DELETE FROM origin_native_receipts WHERE task_id=?', (parent,))
            else:
                binding = json.loads(conn.execute('SELECT binding_json FROM origin_native_receipts WHERE task_id=?',
                                                  (parent,)).fetchone()[0])
                binding[damage]['profile'] = 'unrelated'
                if damage == 'owner':
                    binding['task_record_key'] = 'unrelated/default/' + parent
                conn.execute('UPDATE origin_native_receipts SET binding_json=? WHERE task_id=?',
                             (json.dumps(binding), parent))
            conn.commit()
    result = support.invoke(world, monkeypatch, capsys)
    assert result.get('created') is not True, result
    if damage == 'missing':
        assert result['status'] == 'rejected', result
        assert result['code'] == 'origin_creator_unproven', result
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='origin_creation_intents'").fetchone():
            assert conn.execute('SELECT COUNT(*) FROM origin_creation_intents').fetchone()[0] == 0


@pytest.mark.parametrize('damage', ['task', 'shape', 'sender', 'public'])
def test_invalid_creator_cannot_mint_local_ownership(world, monkeypatch, capsys, damage):
    creator = {'gateway': 'owner', 'profile': 'default', 'board': 'default', 'task_id': 'missing'}
    world['args']['creator'] = creator
    if damage == 'shape':
        creator['extra'] = 'invalid'
    elif damage == 'sender':
        world['from'] = 'gw:worker'
    elif damage == 'public':
        world['args'].pop('creator')
        world['args']['create']['creator_task_id'] = 'missing'
    result = support.invoke(world, monkeypatch, capsys)
    assert result.get('created') is not True, result
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
