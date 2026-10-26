"""Remote session work never joins task identities across owner profiles."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity


def replica(tmp_path, records):
    path = tmp_path / 'hub.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT);'
                         'CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,data TEXT,updated_at INTEGER);')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', '100')])
        for key, data in records:
            db.execute("INSERT INTO replica VALUES ('owner','tasks',?,?,100000)", (key, json.dumps(data)))
    return path


def card(profile, **extra):
    return {'id': 't_same', 'board': 'default', 'profile': profile, 'status': 'running',
            'sessions': [{'gw': 'origin', 'profile': 'default', 'session': 's1', 'rel': 'shepherd'}], **extra}


def test_qualified_profiles_cannot_overwrite_each_other(tmp_path):
    path = replica(tmp_path, [('default/default/t_same', card('default')),
                              ('work/default/t_same', card('work'))])
    rows = activity._read_remote_links(path, now=100)
    assert {r['key'] for r in rows} == {'owner/default/default/t_same', 'owner/work/default/t_same'}
    assert {r['profile'] for r in rows} == {'default', 'work'}


def test_legacy_terminal_record_is_unknown_not_superseded_by_qualified_collision(tmp_path):
    path = replica(tmp_path, [('default/t_same', card('default', status='done')),
                              ('default/default/t_same', card('default', status='done'))])
    rows = activity._read_remote_links(path, now=100)
    assert len(rows) == 2
    assert {r['key'] for r in rows} == {'owner/default/t_same', 'owner/default/default/t_same'}
    assert all(r['status'] == 'unknown' and r['reported_status'] == 'done' for r in rows)
    assert all(activity._replica_bucket(r) == 'active' for r in rows)
    legacy = next(r for r in rows if r['profile'] is None)
    assert legacy['ownership_state'] == 'unknown'


def test_origin_profile_and_relation_are_part_of_the_session_join(tmp_path, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    links = [{'gw': 'origin', 'profile': 'work', 'session': 'foreign', 'rel': 'shepherd'},
             {'gw': 'origin', 'profile': 'default', 'session': 'creator', 'rel': 'created'},
             {'gw': 'origin', 'profile': None, 'session': 'ambiguous', 'rel': 'shepherd'},
             {'gw': 'origin', 'profile': 'default', 'session': 's1', 'rel': 'shepherd'}]
    path = replica(tmp_path, [('work/default/t_same', card('work', sessions=links))])
    assert {r['session'] for r in activity._read_remote_links(path, now=100)} == {'s1'}


def test_descendants_stay_in_the_roots_owner_profile(tmp_path):
    path = replica(tmp_path, [
        ('work/default/t_root', card('work', id='t_root', children=['t_same'])),
        ('work/default/t_same', card('work', sessions=[])),
        ('default/default/t_same', card('default', sessions=[], status='blocked', block_kind='needs_input')),
    ])
    rows = activity._read_remote_links(path, now=100)
    assert {r['key'] for r in rows} == {'owner/work/default/t_root', 'owner/work/default/t_same'}
    assert all(r['status'] == 'running' for r in rows)


@pytest.mark.parametrize('payload', ['not json', '[]', '{}', 'null'])
def test_malformed_replica_row_cannot_be_a_successful_empty_projection(tmp_path, payload):
    path = replica(tmp_path, [])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES ('owner','tasks','work/default/t_same',?,100000)", (payload,))
    with pytest.raises(ValueError):
        activity._read_remote_links(path, now=100)
