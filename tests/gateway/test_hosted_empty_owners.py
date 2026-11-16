"""Empty canonical ownership skips room discovery, not supervisor maintenance."""
from contextlib import ExitStack, contextmanager
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import hosted_rooms
from gateway.session_hosted_service import CanonicalHostedRoomService, _OWNER
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.hosted_room_service import HostedRoomService


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with ExitStack() as stack:
        def create(profile='default'):
            home = tmp_path / profile
            home.mkdir()
            db = stack.enter_context(SessionDB(home / 'state.db'))
            authority = SimpleNamespace(
                db=db, profile_id=str(home),
                epoch=begin_runtime_epoch(db, instance_id='empty-owners-test'))
            return CanonicalHostedRoomService(authority, None)
        yield create


def room(service, room_id, *, owner=False, gateway=None):
    if owner:
        service.authorize_room('alice', room_id, create=True)
    return hosted_rooms.create_room(
        service.db_path, room_id=room_id, name=room_id,
        members=[{'member_id': 'a', 'profile': 'default', 'handle': 'a'},
                 {'member_id': 'b', 'profile': 'default', 'handle': 'b'}],
        authority_gateway_id=gateway or hosted_rooms.local_authority_gateway_id())


def discovery_spies(monkeypatch):
    original = HostedRoomService.bindings
    superclass = Mock(side_effect=original)
    monkeypatch.setattr(HostedRoomService, 'bindings', lambda self: superclass(self))
    listing = Mock(wraps=hosted_rooms.list_rooms)
    monkeypatch.setattr(hosted_rooms, 'list_rooms', listing)
    return superclass, listing


def test_empty_owners_skip_discovery_with_historical_rooms(services, monkeypatch):
    service = services()
    room(service, 'historical')
    # Near-prefix metadata cannot turn historical rooms into canonical ownership.
    service.authority.db._execute_write(lambda c: c.executemany(
        'INSERT INTO state_meta(key,value) VALUES (?,?)',
        [(_OWNER.upper() + 'historical', 'alice'),
         ('gateway.hosted.owner.v10:historical', 'alice')]))
    assert [r['room_id'] for r in hosted_rooms.list_rooms(service.db_path)] == ['historical']
    superclass, listing = discovery_spies(monkeypatch)

    assert service.bindings() == ()
    assert service.bindings() == ()
    assert (superclass.call_count, listing.call_count) == (0, 0)


def test_same_instance_observes_first_owner_and_final_deletion(services, monkeypatch):
    service = services()
    superclass, listing = discovery_spies(monkeypatch)
    observations = [service.bindings()]
    counts = [(superclass.call_count, listing.call_count)]
    room(service, 'first', owner=True)
    observations.append(service.bindings())
    counts.append((superclass.call_count, listing.call_count))
    service.authority.db._execute_write(lambda c: c.execute(
        'DELETE FROM state_meta WHERE key=?', (_OWNER + 'first',)))
    observations.append(service.bindings())
    counts.append((superclass.call_count, listing.call_count))

    assert tuple(tuple(b.room_id for b in result) for result in observations) == ((), ('first',), ())
    assert counts == [(0, 0), (1, 1), (1, 1)]


def test_empty_profile_does_not_borrow_another_profiles_owners(services, monkeypatch):
    empty, owned = services('empty'), services('owned')
    room(empty, 'shared')
    room(owned, 'shared', owner=True)
    superclass, listing = discovery_spies(monkeypatch)

    observations = (empty.bindings(), owned.bindings(), empty.bindings())

    assert tuple(tuple(b.room_id for b in result) for result in observations) == ((), ('shared',), ())
    assert superclass.call_args_list == [((owned,), {})]
    assert listing.call_args_list == [((owned.db_path,), {})]


@pytest.mark.parametrize('failure_at', ['enter', 'query'])
def test_owner_read_errors_propagate_without_discovery(services, monkeypatch, failure_at):
    service = services()
    superclass, listing = discovery_spies(monkeypatch)
    failure = sqlite3.OperationalError('owner read unavailable')

    @contextmanager
    def broken_read():
        if failure_at == 'enter':
            raise failure
        yield SimpleNamespace(execute=Mock(side_effect=failure))

    monkeypatch.setattr(service.authority.db, '_read_ctx', broken_read)
    with pytest.raises(sqlite3.OperationalError) as caught:
        service.bindings()
    assert caught.value is failure
    assert (superclass.call_count, listing.call_count) == (0, 0)


def test_nonempty_owners_do_not_hide_superclass_errors(services, monkeypatch):
    service = services()
    room(service, 'owned', owner=True)
    failure = RuntimeError('room discovery unavailable')
    superclass = Mock(side_effect=failure)
    monkeypatch.setattr(HostedRoomService, 'bindings', superclass)

    with pytest.raises(RuntimeError) as caught:
        service.bindings()
    assert caught.value is failure
    superclass.assert_called_once_with()


def test_nonempty_bindings_preserve_real_filtering_and_order(services):
    service = services()
    for room_id in ('a', 'z', 'tie_b', 'tie_a', 'unowned', 'disbanded', 'foreign'):
        created = room(service, room_id, owner=room_id != 'unowned',
                       gateway='foreign-gateway' if room_id == 'foreign' else None)
        if room_id == 'disbanded':
            hosted_rooms.disband_room(
                service.db_path, room_id=room_id,
                expected_gateway_id=created['authority_gateway_id'],
                expected_epoch=created['authority_epoch'])
    service.authority.db._execute_write(lambda c: c.executemany(
        'UPDATE hosted_rooms SET updated_at=? WHERE room_id=?',
        [(10, 'a'), (30, 'z'), (20, 'tie_b'), (20, 'tie_a'),
         (50, 'unowned'), (60, 'foreign')]))
    base = HostedRoomService.bindings(service)
    assert tuple(b.room_id for b in base) == ('unowned', 'z', 'tie_a', 'tie_b', 'a')
    expected = tuple(b for b in base if b.room_id != 'unowned')
    assert service.bindings() == expected
    assert tuple(b.room_id for b in expected) == ('z', 'tie_a', 'tie_b', 'a')


def test_empty_supervisor_keeps_real_cycles_and_maintenance(services, monkeypatch):
    service = services()
    room(service, 'historical')
    superclass, listing = discovery_spies(monkeypatch)
    runtime = service.runtime
    maintenance = Mock(wraps=runtime.maintain)

    def maintain_and_stop():
        maintenance()
        if maintenance.call_count == 2:
            runtime._stop.set()
        runtime._wake.set()

    runtime.maintain = maintain_and_stop
    # Exercise the real supervisor branch synchronously, without a sleeping thread.
    runtime._thread = threading.current_thread()
    try:
        runtime._worker_loop()
    finally:
        runtime._thread = None

    assert maintenance.call_count == 2
    assert runtime.status()['cycles'] == 2
    assert runtime.status()['last_error'] is None
    assert runtime.status()['leased_rooms'] == ()
    assert (superclass.call_count, listing.call_count) == (0, 0)
