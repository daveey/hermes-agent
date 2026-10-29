"""A remembered remote row whose card a fresh replica proves lives on another gateway is retired. [daveey]

Live incident (10-29): the hub's worker node ran MacBook card t_1f359ff1 from a copy in the hub's board file, and the
hub collector published that copy as ``hub/default/default/t_1f359ff1`` with the MacBook chat's shepherd link. After
the copy was evicted the record vanished; absence is never closure, so the MacBook kept the row open (stale) forever
and its chat counted a card the named owner never had.
"""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_session_activity as activity

LINK = {'gw': 'origin', 'profile': 'default', 'session': 's1', 'rel': 'shepherd'}


def _card(gw, *, status='running', created_at=1, sessions=(LINK,)):
    return {'id': 't_1', 'board': 'default', 'gateway': gw, 'profile': 'default', 'status': status,
            'created_at': created_at, 'created_by': 'default', 'sessions': list(sessions)}


def _write(path, records, *, beats):
    now = time.time()
    with sqlite3.connect(path) as db:
        db.executescript('DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS replica;'
                         'CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT);'
                         'CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,version INTEGER,data TEXT,updated_at INTEGER,'
                         'PRIMARY KEY (gw,coll,key));')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', str(now))])
        for gw, beat in beats.items():
            info = {'gateway': gw, 'task_scopes': [
                {'profile': 'default', 'board': 'default', 'state': 'ok', 'observed_at': now - beat}]}
            db.execute("INSERT INTO replica VALUES (?,'gateway','info',1,?,0)", (gw, json.dumps(info)))
        for gw, data in records:
            db.execute("INSERT INTO replica VALUES (?,'tasks','default/default/t_1',1,?,?)",
                       (gw, json.dumps(data), int(now * 1000)))


@pytest.fixture
def origin(tmp_path, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    hub = tmp_path / 'hub.db'
    monkeypatch.setattr(activity, '_hub_db_path', lambda: hub)
    saved = {'cards': None}

    def snapshot(cards=None):
        if cards is None:
            return saved['cards']
        saved['cards'] = cards
        return cards

    monkeypatch.setattr(activity, '_remote_snapshot', snapshot)
    activity._reset_cache_for_tests()
    # The worker node's copy is published under the hub; the real owner (this gateway) publishes its own card.
    _write(hub, [('hub', _card('hub')), ('origin', _card('origin', sessions=()))], beats={'hub': 5, 'origin': 5})
    assert [(c['key'], c['unfinished']) for c in activity._legacy_remote_links()] == [
        ('hub/default/default/t_1', True)]
    yield hub
    activity._reset_cache_for_tests()


def _open(rows):
    return [(c['key'], c.get('stale')) for c in rows if c.get('unfinished')]


@pytest.mark.parametrize('restart', [False, True], ids=['same-process', 'cold-start-from-snapshot'])
def test_a_worker_node_copy_gone_from_a_fresh_replica_is_retired(origin, restart):
    _write(origin, [('origin', _card('origin', status='done', sessions=()))], beats={'hub': 5, 'origin': 5})
    if restart:
        activity._reset_cache_for_tests()  # the remembered row now comes only from the durable snapshot
    assert _open(activity._legacy_remote_links()) == []


@pytest.mark.parametrize('why, records, beats', [
    ('named owner offline: its silence proves nothing', [('origin', _card('origin', sessions=()))],
     {'hub': 3600, 'origin': 5}),
    ('no other gateway publishes the card', [], {'hub': 5, 'origin': 5}),
    ('another gateway publishes a different card under the same id', [('origin', _card('origin', created_at=2,
                                                                                         sessions=()))],
     {'hub': 5, 'origin': 5}),
])
def test_an_unproven_move_keeps_the_row_open(origin, why, records, beats):
    _write(origin, records, beats=beats)
    assert _open(activity._legacy_remote_links()) == [('hub/default/default/t_1', True)], why
