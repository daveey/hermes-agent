"""Compose creator parking, origin admission and real owner creation entries."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_sync_client
from hermes_cli import kanban_origin_ledger as ledger
from hermes_state import SessionDB
from tests.hermes_cli import test_kanban_origin_owner as owner
from tests.hermes_cli import test_origin_forward_adapter as forward

world = owner.world


def test_creator_park_waits_for_exactly_one_admitted_child(world, tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban_capability_registry as registry
    home = tmp_path
    path = home / 'hub' / 'hub.db'
    path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(registry, 'hub_db_path', lambda: path)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT);"
                         "INSERT INTO meta VALUES('gateway','owner');"
                         "CREATE TABLE sent(id TEXT PRIMARY KEY,state TEXT,result TEXT,body TEXT);")
    state = SessionDB(db_path=home / 'state.db')
    state.create_session('original', 'cli')
    state.close()
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='creator')
        assert kb.claim_task(conn, creator)
        run = kb._current_run_id(conn, creator)
        uid = conn.execute('SELECT uid FROM task_runs WHERE id=?', (run,)).fetchone()[0]
    binding = {'binding_id': 'binding-creator', 'version': 1, 'publication_version': 1,
               'origin': {'gateway': 'owner', 'profile': 'default', 'session': 'original'},
               'owner': {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': creator},
               'task_record_key': 'default/default/' + creator}
    with ledger._db(home / 'kanban' / 'origin-ledger.db') as db:
        db.execute('INSERT INTO origin_obligations VALUES(?,?,?,?,?,?,?)',
                   (ledger._binding_key(binding), ledger.binding_hash(binding), json.dumps(binding),
                    'original', '{}', 1, 0))
        db.commit()
    request = {'id': 'placement:worker:delayed', 'from': 'gw:worker', 'to_gw': 'owner',
               'coll': 'sessions', 'key': 'default/original', 'action': 'origin-forward',
               'args': {'origin': binding['origin'], 'creator': binding['owner'], 'run_uid': uid,
                        'destination': {'gateway': 'owner', 'profile': 'default', 'board': 'default'},
                        'create': {'title': 'child', 'assignee': 'default', 'session_id': 'creator'}}}
    frozen = json.dumps(request)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO sent VALUES(?,?,?,?)', (request['id'], 'sent', '{}', frozen))
    # The genuine schedule entry rolls back rather than releasing admission custody.
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='origin_forward_pending'):
            kb.schedule_task(conn, creator, expected_run_id=run, reason='calendar window', clock_only=True,
                             awaiting={'kind': 'clock', 'why': 'calendar window'})
        assert kb.latest_run(conn, creator).ended_at is None

    def lease(*args, **kwargs):
        with kbc.connect_closing() as conn:
            assert kb.latest_run(conn, creator).ended_at is None
        return {'table': 'leases', 'rows': [{'task_id': creator, 'holder': 'worker', 'run_uid': uid,
                                            'expires': 9999999999999}], 'more': False}

    monkeypatch.setattr(kanban_sync_client, 'load_identity', lambda: None)
    monkeypatch.setattr(kanban_sync_client.Hub, 'snapshot_page', lease)
    monkeypatch.setattr(kanban_sync_client.Hub, 'machines', lambda self: {
        'me': 'owner', 'machines': [{'name': 'owner', 'state': 'active'}]})
    with monkeypatch.context() as scoped:
        prepared = forward.invoke(None, scoped, capsys, request)
    assert prepared['status'] == 'prepared', prepared
    outbound = prepared['outbound'] | {'from': 'gw:owner'}
    first = owner.invoke(outbound, monkeypatch, capsys)
    second = owner.invoke(outbound, monkeypatch, capsys)
    assert first.get('created') is True, first
    assert second['task_id'] == first['task_id']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT body FROM sent').fetchone()[0] == frozen
        db.execute('UPDATE sent SET state=?,result=?', ('done', json.dumps(first)))
    with kbc.connect_closing() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2
        assert kb.schedule_task(conn, creator, expected_run_id=run, reason='calendar window', clock_only=True,
                                awaiting={'kind': 'clock', 'why': 'calendar window'})
        assert kb.get_task(conn, creator).status == 'scheduled'
