"""Which gateways publish a card: only the projection's own cards are read, answers fixed per projection. [daveey]"""
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


def reader(db, listed=frozenset({'peer'}), wanted=(('board', 'new'), ('board', 'existing'))):
    return _Publications(db, listed, wanted)


def owner(tid='new'):
    return {'gateway': 'gone', 'board': 'board', 'task_id': tid}


def put(db, key='p/board/new', created=1, gw='peer'):
    db.execute("INSERT OR REPLACE INTO replica VALUES(?,'tasks',?,?)", (gw, key, json.dumps({'created_at': created})))


def key_reads(db):
    reads = []
    db.set_trace_callback(reads.append)
    return lambda: [sql for sql in reads if 'json_each' in sql]


def test_wanted_cards_are_read_in_one_query_and_nothing_else_is_returned(source):
    for i in range(50):
        put(source, f'p/board/other{i}')
    put(source)
    reads = key_reads(source)
    current = reader(source)
    assert current.has_other(owner(), {'created_at': 1})
    assert not current.has_other(owner('existing'), {'created_at': 1})
    assert len(reads()) == 1  # both wanted cards from the first ask's single query
    assert set(current.found) == {('board', 'new'), ('board', 'existing')}


def test_card_outside_wanted_gets_its_own_exact_lookup(source):
    put(source, 'p/board/late')
    reads = key_reads(source)
    current = reader(source)
    assert current.has_other(owner('late'), {'created_at': 1})
    assert current.has_other(owner('late'), {'created_at': 1})
    assert len(reads()) == 1
    assert ('board', 'new') not in current.found  # the unasked batch was not read for an outside card


def test_answers_fixed_within_projection_but_fresh_between(source):
    current = reader(source)
    assert not current.has_other(owner(), {'created_at': 1})
    put(source)
    assert not current.has_other(owner(), {'created_at': 1})
    assert reader(source).has_other(owner(), {'created_at': 1})


def test_payload_changes_are_live(source):
    put(source)
    assert reader(source).has_other(owner(), {'created_at': 1})
    put(source, created=2)
    second = reader(source)
    assert not second.has_other(owner(), {'created_at': 1})
    assert second.has_other(owner(), {'created_at': 2})


def test_deletion_and_reinsertion(source):
    put(source)
    source.execute("DELETE FROM replica WHERE key='p/board/new'")
    assert not reader(source).has_other(owner(), {'created_at': 1})
    put(source, created=2)
    assert reader(source).has_other(owner(), {'created_at': 2})


def test_listed_gateway_changes(source):
    put(source)
    assert reader(source).has_other(owner(), {'created_at': 1})
    assert not reader(source, frozenset()).has_other(owner(), {'created_at': 1})


def test_owner_gateway_own_record_does_not_count(source):
    put(source, gw='gone')
    assert not reader(source, frozenset({'peer', 'gone'})).has_other(owner(), {'created_at': 1})


@pytest.mark.parametrize('created', [None, True, [], {}])
def test_invalid_created_does_not_read(source, created):
    source.close()
    assert not reader(source).has_other(owner(), {'created_at': created})


def test_failed_read_raises_and_is_not_cached(source):
    class Broken:
        def execute(self, *_):
            raise sqlite3.OperationalError('injected')

    current = reader(Broken())
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        current.has_other(owner(), {'created_at': 1})
    assert current.found is None
    current.source = source
    put(source)
    assert current.has_other(owner(), {'created_at': 1})


def test_malformed_keys_follow_the_three_part_rule(source):
    keys = ['empty', 'too/many/parts/here', '/board/empty-profile', 'p//empty-board', 'p/board/',
            'π/board/unicode', 'p/board/under_score', 'p/board/per%cent', 'board/new']
    for key in keys:
        put(source, key)
    wanted = {('board', 'empty-profile'), ('', 'empty-board'), ('board', ''), ('board', 'unicode'),
              ('board', 'under_score'), ('board', 'per%cent'), ('board', 'new'), ('parts', 'here'),
              ('board', 'under-score'), ('board', 'perXcent'), ('many/parts', 'here')}
    current = reader(source, wanted=wanted)
    expected = {}
    for key, in source.execute("SELECT key FROM replica WHERE gw='peer' AND coll='tasks' ORDER BY key"):
        parts = str(key).split('/')
        if len(parts) == 3 and (parts[1], parts[2]) in wanted:
            expected.setdefault((parts[1], parts[2]), []).append(('peer', str(key)))
    actual = {pair: list(current._publishers(*pair)) for pair in sorted(wanted)}
    assert {pair: rows for pair, rows in actual.items() if rows} == expected
    # LIKE/GLOB wildcards in ids are literal here, and 'board/new' (two parts) is not a publication.
    assert actual[('board', 'under-score')] == [] and actual[('board', 'perXcent')] == []
    assert actual[('board', 'new')] == []
