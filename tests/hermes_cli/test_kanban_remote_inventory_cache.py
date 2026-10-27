"""The remote inventory is projected once per scope and store state, and its durable snapshot never fails a read."""
import sqlite3
import threading

import pytest

import hermes_constants
from hermes_cli import kanban_origin_adapter as adapter
from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_session_activity as activity
from hermes_cli import profiles
from tests.waiting import wait_until


@pytest.fixture
def projection(tmp_path, monkeypatch):
    """A receipt ledger whose (stubbed) projection counts how often it runs."""
    (tmp_path / 'kanban').mkdir()
    (tmp_path / 'kanban/origin-ledger.db').touch()
    board = tmp_path / 'kanban.db'
    with sqlite3.connect(board) as db:
        db.execute('CREATE TABLE tasks (id TEXT, status TEXT)')
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: None)
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [board])
    monkeypatch.setattr(activity, '_legacy_remote_links', list)
    row = dict(gateway='owner', profile='work', board='build', id='t_one', key='owner/work/build/t_one',
               session='s_one', origin_session='s_one', origin_gateway='origin', origin_profile='default',
               status='running', unfinished=True, stale=False)
    calls = {'n': 0, 'gate': None, 'fail': False}

    def project(*args, **kwargs):
        calls['n'] += 1
        if calls['gate'] is not None:
            calls['gate'].wait(timeout=10)  # held open until the test has every reader waiting
        if calls['fail']:
            calls['fail'] = False
            raise sqlite3.OperationalError('database is locked')
        return [dict(row)]

    monkeypatch.setattr(ledger, 'project', project)
    activity._reset_cache_for_tests()
    yield calls, board
    activity._reset_cache_for_tests()


def test_unchanged_stores_share_one_projection(projection):
    calls, _ = projection
    first = activity._remote_links()
    assert activity._remote_links() == first
    assert activity.session_tasks(['s_one'])  # another reader of the same inventory
    assert calls['n'] == 1


def test_a_store_change_reprojects(projection):
    calls, board = projection
    activity._remote_links()
    with sqlite3.connect(board) as db:
        db.execute("INSERT INTO tasks VALUES ('t_two', 'ready')")
    activity._remote_links()
    assert calls['n'] == 2


def test_ttl_expiry_reprojects(projection, monkeypatch):
    calls, _ = projection
    monkeypatch.setattr(activity, '_CACHE_TTL_S', 0.0)
    activity._remote_links()
    activity._remote_links()
    assert calls['n'] == 2


def test_concurrent_readers_wait_for_one_projection(projection):
    calls, _ = projection
    gate, arrived, results = threading.Event(), [], []
    calls['gate'] = gate

    def reader():
        arrived.append(1)
        results.append(activity._remote_links())

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    wait_until(lambda: len(arrived) == 8 and calls['n'] >= 1, what='readers started, one projecting')
    try:  # without single-flight every reader starts its own projection while the first is held open
        wait_until(lambda: calls['n'] == 8, timeout=1.0)
    except AssertionError:
        pass
    gate.set()
    for t in threads:
        t.join()
    assert len(results) == 8 and all(r == results[0] for r in results)
    assert calls['n'] == 1


def test_a_failed_projection_is_not_cached(projection):
    calls, _ = projection
    calls['fail'] = True
    with pytest.raises(sqlite3.OperationalError):
        activity._remote_links()
    assert activity._remote_links()
    assert calls['n'] == 2


@pytest.fixture
def replica(tmp_path, monkeypatch):
    """A readable replica (stubbed read) plus a durable snapshot store that records writes."""
    hub = tmp_path / 'hub.db'
    hub.touch()
    card = dict(id='t_one', key='owner/work/build/t_one', session='s_one', status='running', unfinished=True)
    monkeypatch.setattr(activity, '_hub_db_path', lambda: hub)
    monkeypatch.setattr(activity, '_read_remote_links', lambda path, **kw: [dict(card)])
    writes = []

    def snapshot(cards=None):
        if cards is None:
            return None
        writes.append(cards)
        if snapshot.locked:
            raise sqlite3.OperationalError('database is locked')
        return cards

    snapshot.locked = False
    monkeypatch.setattr(activity, '_remote_snapshot', snapshot)
    activity._reset_cache_for_tests()
    yield snapshot, writes, card
    activity._reset_cache_for_tests()


def test_locked_snapshot_write_does_not_fail_the_read(replica):
    snapshot, writes, card = replica
    snapshot.locked = True
    assert activity._legacy_remote_links() == [card]
    assert len(writes) == 1
    snapshot.locked = False
    activity._legacy_remote_links()  # the unsaved inventory is retried, not forgotten
    assert len(writes) == 2


def test_unchanged_inventory_is_written_once(replica):
    _, writes, _ = replica
    activity._legacy_remote_links()
    activity._legacy_remote_links()
    assert len(writes) == 1
