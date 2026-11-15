"""Canonical owner discovery must use the existing binary primary-key range."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from gateway.session_hosted_service import CanonicalHostedRoomService, _OWNER
from tui_gateway.hosted_room_service import HostedRoomService


@contextmanager
def owner_service(path, monkeypatch, room_ids):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT)')
    service = object.__new__(CanonicalHostedRoomService)
    @contextmanager
    def read():
        yield conn
    service.authority = SimpleNamespace(db=SimpleNamespace(_read_ctx=read))
    bindings = tuple(SimpleNamespace(room_id=r) for r in room_ids)
    monkeypatch.setattr(HostedRoomService, 'bindings', lambda self: bindings)
    try:
        yield service, conn
    finally:
        conn.close()


def keys(conn, values):
    conn.executemany('INSERT INTO state_meta VALUES (?, ?)', [(k, 'alice') for k in values])
    conn.commit()


def ids(service):
    return tuple(b.room_id for b in service.bindings())


def test_canonical_prefix_suffix_and_order(monkeypatch, tmp_path):
    # Unicode suffix coverage is a storage differential, not permission to create
    # identifiers rejected by the public room API. Never normalize stored suffixes.
    rooms = ('z', 'Room_A:Z', 'room_a:z', 'é雪', 'É雪', 'unowned', 'variant')
    with owner_service(tmp_path / 'state.db', monkeypatch, rooms) as (service, conn):
        canonical = [_OWNER + r for r in rooms[:4]]
        near = ['gateway.hosted.owner.v1', 'gateway.hosted.owner.v1/x',
                'gateway.hosted.owner.v1;', 'gateway.hosted.owner.v1;unowned',
                'gateway.hosted.owner.v10:unowned', 'gateway.hosted.owner.v2:unowned']
        keys(conn, canonical + near + [_OWNER, _OWNER + 'absent',
                                      _OWNER.upper() + 'variant',
                                      'Gateway.hosted.owner.v1:variant',
                                      _OWNER.upper() + 'z'])
        assert ids(service) == rooms[:4]
        assert service._owner('Room_A:Z') == 'alice'
        from hermes_state_runtime import RuntimeStoreError
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            service._owner('variant')
        # On canonical inputs, the original LIKE oracle has exactly the same
        # suffixes. Alternate-case prefixes were never valid authorization keys.
        conn.execute('DELETE FROM state_meta WHERE key IN (?, ?, ?)',
                     (_OWNER.upper() + 'variant', 'Gateway.hosted.owner.v1:variant', _OWNER.upper() + 'z'))
        old = {r[0][len(_OWNER):] for r in conn.execute(
            'SELECT key FROM state_meta WHERE key LIKE ?', (_OWNER + '%',))}
        assert ids(service) == tuple(r for r in rooms if r in old)


def test_next_poll_observes_committed_owners_and_profile_isolation(monkeypatch, tmp_path):
    with owner_service(tmp_path / 'a.db', monkeypatch, ('a', 'b')) as (a, ca):
        with owner_service(tmp_path / 'b.db', monkeypatch, ('a', 'b')) as (b, cb):
            keys(ca, [_OWNER + 'a'])
            keys(cb, [_OWNER + 'b'])
            assert ids(a) == ('a',)
            assert ids(b) == ('b',)
            with sqlite3.connect(tmp_path / 'a.db') as writer:
                writer.execute('DELETE FROM state_meta WHERE key=?', (_OWNER + 'a',))
                writer.execute('INSERT INTO state_meta VALUES (?, ?)', (_OWNER + 'b', 'alice'))
            assert ids(a) == ('b',)
            assert ids(b) == ('b',)
            ca.execute('DELETE FROM state_meta')
            ca.commit()
            assert ids(a) == ()
            assert ids(b) == ('b',)


def test_complete_bindings_native_and_imported_ownership(tmp_path, monkeypatch):
    from gateway import hosted_rooms
    from gateway.hosted_rooms_gateway_import import import_standalone_rooms
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    members = [{'member_id': 'a', 'profile': 'default', 'handle': 'a'},
               {'member_id': 'b', 'profile': 'default', 'handle': 'b'}]
    local = hosted_rooms.local_authority_gateway_id()
    with SessionDB(tmp_path / 'state.db') as db:
        authority = SimpleNamespace(db=db, profile_id=str(tmp_path),
                                    epoch=begin_runtime_epoch(db, instance_id='test'))
        service = CanonicalHostedRoomService(authority, None)
        for room, owner, gateway in [('Z', True, local), ('a', True, local),
                                     ('unowned', False, local), ('foreign', True, 'foreign')]:
            if owner:
                service.authorize_room('alice', room, create=True)
            hosted_rooms.create_room(db.db_path, room_id=room, name=room,
                                     members=members, authority_gateway_id=gateway)
        # Equal timestamps make ordering explicit and different from owner-key order.
        db._execute_write(lambda c: c.execute('UPDATE hosted_rooms SET updated_at=1'))
        assert ids(service) == ('Z', 'a')
        source = tmp_path / 'shared-state.db'
        hosted_rooms.create_room(source, room_id='Imported', name='Imported',
                                 members=members, authority_gateway_id=local)
        assert import_standalone_rooms(tmp_path / 'state.db') == 1
        assert ids(service) == ('Imported', 'Z', 'a')
        db._execute_write(lambda c: c.execute('DELETE FROM state_meta WHERE key=?', (_OWNER + 'Z',)))
        assert ids(service) == ('Imported', 'a')


@pytest.mark.parametrize('unrelated', [1000, 20000, 100000])
def test_owner_lookup_vm_work_is_bounded(monkeypatch, tmp_path, unrelated):
    with owner_service(tmp_path / 'state.db', monkeypatch, ('room',)) as (service, conn):
        keys(conn, [f'unrelated:{i:08}' for i in range(unrelated)] + [_OWNER + 'room'])
        steps = []
        statements = []
        conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
        conn.set_trace_callback(statements.append)
        assert ids(service) == ('room',)
        conn.set_progress_handler(None, 0)
        conn.set_trace_callback(None)
        assert len(steps) < 100, len(steps)
        query, = statements
        plan = ' '.join(r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + query))
        assert 'SEARCH state_meta USING COVERING INDEX' in plan
        assert 'key>?' in plan and 'key<?' in plan
