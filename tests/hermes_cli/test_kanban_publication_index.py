"""Membership reuse must not cache live publication payloads or extend membership."""
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_cli.kanban_origin_ledger import _Publications


@pytest.fixture
def source():
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,data TEXT,PRIMARY KEY(gw,coll,key))')
        db.execute("INSERT INTO replica VALUES('peer','tasks','p/board/existing','{}')")
        yield db


def reader(db, listed=frozenset({'peer'})):
    return _Publications(db, listed)


def owner(tid='new'):
    return {'gateway': 'gone', 'board': 'board', 'task_id': tid}


def put(db, key='p/board/new', created=1):
    db.execute("INSERT OR REPLACE INTO replica VALUES('peer','tasks',?,?)", (key, json.dumps({'created_at': created})))


def test_same_inventory_skips_membership_rebuild(source):
    first = reader(source)._index()
    second = reader(source)._index()
    assert second is first


def test_membership_frozen_within_projection_but_fresh_between(source):
    current = reader(source)
    assert not current.has_other(owner(), {'created_at': 1})
    put(source)
    assert not current.has_other(owner(), {'created_at': 1})
    assert reader(source).has_other(owner(), {'created_at': 1})


def test_payload_changes_are_live_on_reused_membership(source):
    put(source)
    first = reader(source)
    assert first.has_other(owner(), {'created_at': 1})
    put(source, created=2)
    second = reader(source)
    assert not second.has_other(owner(), {'created_at': 1})
    assert second.has_other(owner(), {'created_at': 2})
    assert second._index() is first._index()


def test_deletion_and_reinsertion_invalidate(source):
    put(source)
    old = reader(source)._index()
    source.execute("DELETE FROM replica WHERE key='p/board/new'")
    assert not reader(source).has_other(owner(), {'created_at': 1})
    put(source, created=2)
    assert reader(source).has_other(owner(), {'created_at': 2})
    assert ('board', 'new') in old


def test_listed_gateway_changes_invalidate(source):
    put(source)
    assert reader(source).has_other(owner(), {'created_at': 1})
    assert not reader(source, frozenset()).has_other(owner(), {'created_at': 1})


@pytest.mark.parametrize('created', [None, True, [], {}])
def test_invalid_created_does_not_read(source, created):
    source.close()
    assert not reader(source).has_other(owner(), {'created_at': created})


def test_error_keeps_partial_index(source):
    class Broken:
        def execute(self, *_):
            yield ('p/board/one',)
            raise sqlite3.OperationalError('injected')

    current = reader(Broken())
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        current._index()
    assert list(current._index()[('board', 'one')]) == [('peer', 'p/board/one')]


def test_malformed_keys_and_order(source):
    keys = ['empty', 'too/many/parts/here', '/board/empty-profile', 'p//empty-board', 'p/board/', 'π/board/unicode']
    for key in keys:
        put(source, key)
    actual = reader(source)._index()
    expected = {}
    for key, in source.execute("SELECT key FROM replica WHERE gw='peer' AND coll='tasks'"):
        parts = str(key).split('/')
        if len(parts) == 3:
            expected.setdefault((parts[1], parts[2]), []).append(('peer', str(key)))
    assert [(k, list(v)) for k, v in actual.items()] == list(expected.items())


def test_reused_index_is_immutable(source):
    current = reader(source)._index()
    with pytest.raises(TypeError):
        current[('board', 'existing')] = ()
    with pytest.raises(TypeError):
        current[('board', 'existing')][0] = ('wrong', 'wrong')
    assert reader(source)._index() is current


@pytest.mark.parametrize('bound,value', [('_MAX_KEYS', 1), ('_MAX_CHARS', 1)])
def test_oversize_drops_retained_entry_but_preserves_answer(source, monkeypatch, bound, value):
    from hermes_cli import kanban_publication_index as membership

    put(source)
    reader(source)._index()
    monkeypatch.setattr(membership, bound, value)
    # New inventory must be admitted independently, never use the old bound's hit.
    put(source, 'q/board/another')
    assert reader(source).has_other(owner(), {'created_at': 1})
    assert membership._memo is None


def test_churn_replaces_single_entry_and_resumes_hits(source):
    from hermes_cli import kanban_publication_index as membership

    for i in range(30):
        put(source, 'p/board/churn' + str(i))
        first = reader(source)._index()
        assert reader(source)._index() is first
        assert membership._memo[1] is first


def test_full_inventory_is_still_read_and_build_skipped(source, monkeypatch):
    from hermes_cli import kanban_publication_index as membership

    monkeypatch.setattr(membership, '_memo', None)
    build = membership.build
    builds = []
    reads = []
    source.set_trace_callback(reads.append)

    def tracked(groups):
        builds.append(1)
        return build(groups)

    monkeypatch.setattr(membership, 'build', tracked)
    reader(source)._index()
    reader(source)._index()
    assert len(builds) == 1
    assert len([sql for sql in reads if 'SELECT key FROM replica' in sql]) == 2


def test_failed_scan_never_publishes_partial_entry(source):
    from hermes_cli import kanban_publication_index as membership

    reader(source)._index()
    old = membership._memo

    class Broken:
        def execute(self, *_):
            yield ('p/board/partial',)
            raise sqlite3.OperationalError('injected')

    failed = reader(Broken())
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        failed._index()
    assert membership._memo is old
    assert ('board', 'partial') in failed._index()
    assert ('board', 'partial') not in reader(source)._index()


def test_non_text_gateway_does_not_reuse_string_equal_inventory():
    from hermes_cli import kanban_publication_index as membership

    membership.derive([(1, ['p/b/t'])])
    assert membership._memo is None
    result = membership.derive([(True, ['p/b/t'])])
    assert result[('b', 't')][0][0] is True
    assert membership._memo is None

    class Gateway(str):
        pass

    seed = membership.derive([('peer', ['p/b/t'])])
    gateway = Gateway('peer')
    special = membership.derive([(gateway, ['p/b/t'])])
    assert special is not seed
    assert special[('b', 't')][0][0] is gateway
    assert membership._memo is None


def test_order_change_is_part_of_inventory(source):
    forward = reader(source)._index()
    source.execute('PRAGMA reverse_unordered_selects=ON')
    put(source)
    reverse = reader(source)._index()
    assert list(reverse) == [('board', 'new'), ('board', 'existing')]
    source.execute('PRAGMA reverse_unordered_selects=OFF')
    forward = reader(source)._index()
    assert list(forward) == [('board', 'existing'), ('board', 'new')]
    assert forward is not reverse


def test_concurrent_different_inventories_are_not_cross_paired():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hermes_cli import kanban_publication_index as membership

    barrier = Barrier(4)

    def collect(i):
        groups = [('peer', ['p/b/' + str(i)])]
        barrier.wait(timeout=10)
        for _ in range(50):
            result = membership.derive(groups)
            assert list(result) == [('b', str(i))]
            assert result[('b', str(i))] == (('peer', 'p/b/' + str(i)),)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(collect, range(4)))


@pytest.mark.parametrize('key', [None, 123, b'p/b/t', b'plain'])
def test_sqlite_storage_types_keep_original_string_normalization(source, key):
    source.execute("INSERT INTO replica VALUES('peer','tasks',?,'{}')", (key,))
    actual = reader(source)._index()
    expected = {}
    for value, in source.execute("SELECT key FROM replica WHERE gw='peer' AND coll='tasks'"):
        parts = str(value).split('/')
        if len(parts) == 3:
            expected.setdefault((parts[1], parts[2]), []).append(('peer', str(value)))
    assert [(k, list(v)) for k, v in actual.items()] == list(expected.items())

