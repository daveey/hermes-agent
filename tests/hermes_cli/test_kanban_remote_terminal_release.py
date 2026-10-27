"""Remote cards their owner attests closed leave the origin's open counts; unproven ones stay. [daveey]"""
import hashlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.kanban_origin_ledger import _json

NOW = 10_000.0
LINK = {'gw': 'origin', 'profile': 'default', 'session': 's1', 'rel': 'shepherd'}


def observed(task, **tamper):
    """An owner-collector publication: identity fields plus the hash-bound observation."""
    payload = {k: v for k, v in task.items() if k != 'origin_observation'}
    evidence = {'schema': 1, 'task_hash': hashlib.sha256(_json(payload).encode()).hexdigest(), 'verified': False}
    return {**payload, **tamper, 'origin_observation': evidence}


def card(status='done', *, tid='t_1', board='default', **extra):
    return {'id': tid, 'board': board, 'gateway': 'owner', 'profile': 'default', 'status': status,
            'created_at': 1, 'created_by': 'default', 'sessions': [LINK], **extra}


def replica(tmp_path, records, *, last_ok=NOW, beat=NOW, scopes=None):
    path = tmp_path / 'hub.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT);'
                         'CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,version INTEGER,data TEXT,updated_at INTEGER,'
                         'PRIMARY KEY (gw,coll,key));')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', str(last_ok))])
        info = {'gateway': 'owner', 'task_scopes': scopes if scopes is not None else [
            {'profile': 'default', 'board': 'default', 'state': 'ok', 'observed_at': beat}]}
        db.execute("INSERT INTO replica VALUES ('owner','gateway','info',1,?,0)", (json.dumps(info),))
        for key, data in records:
            db.execute("INSERT INTO replica VALUES ('owner','tasks',?,1,?,?)", (key, json.dumps(data), int(NOW * 1000)))
    return path


def update(path, key, data):
    with sqlite3.connect(path) as db:
        db.execute("UPDATE replica SET data=?, version=version+1 WHERE coll='tasks' AND key=?", (json.dumps(data), key))


@pytest.fixture(autouse=True)
def _origin_profile(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')


def counted(rows):
    return [r for r in rows if activity._replica_bucket(r) is not None]


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_owner_attested_terminal_card_is_an_explicit_released_row(tmp_path, status):
    rows = activity._read_remote_links(replica(tmp_path, [('default/default/t_1', observed(card(status)))]), now=NOW)
    assert [(r['key'], r['status'], r['unfinished'], r['released']) for r in rows] == [
        ('owner/default/default/t_1', status, False, True)]
    assert counted(rows) == []


def test_done_on_owner_leaves_the_count_within_one_sync(tmp_path):
    path = replica(tmp_path, [('default/default/t_1', observed(card('running')))])
    assert len(counted(activity._read_remote_links(path, now=NOW))) == 1
    update(path, 'default/default/t_1', observed(card('done')))
    assert counted(activity._read_remote_links(path, now=NOW)) == []


@pytest.mark.parametrize('why, kwargs, record', [
    ('hub sync stale', {'last_ok': NOW - 600}, None),
    ('owner offline: scope heartbeat old', {'beat': NOW - 600}, None),
    ('owner never reported this board', {'scopes': []}, None),
    ('scope unreadable', {'scopes': [{'profile': 'default', 'board': 'default', 'state': 'error', 'observed_at': NOW}]}, None),
    ('ambiguous scope', {'scopes': [{'profile': 'default', 'board': 'default', 'state': 'ok', 'observed_at': NOW}] * 2}, None),
    ('no owner observation', {}, card('done')),
    ('observation does not hash the record', {}, observed(card('done'), title='edited in transit')),
    ('identity fields disagree with key', {}, observed(card('done', profile='work'))),
    ('ledger-bound card closed without verified completion', {}, observed(card('done', origin_bindings=[{'b': 1}]))),
])
def test_unproven_terminal_card_stays_unfinished(tmp_path, why, kwargs, record):
    record = record if record is not None else observed(card('done'))
    rows = activity._read_remote_links(replica(tmp_path, [('default/default/t_1', record)], **kwargs), now=NOW)
    assert [(r['status'], r['reported_status'], r['unfinished']) for r in rows] == [('unknown', 'done', True)], why
    assert len(counted(rows)) == 1, why


def test_legacy_alias_folds_into_its_proven_twin(tmp_path):
    legacy_link = {**LINK, 'session': 's_old'}
    legacy = {k: v for k, v in card('done', sessions=[legacy_link]).items() if k not in ('gateway', 'profile')}
    path = replica(tmp_path, [('default/t_1', legacy), ('default/default/t_1', observed(card('done')))])
    rows = activity._read_remote_links(path, now=NOW)
    assert {(r['session'], r['key'], r['legacy_key'], r['released']) for r in rows} == {
        ('s1', 'owner/default/default/t_1', 'owner/default/t_1', True),
        ('s_old', 'owner/default/default/t_1', 'owner/default/t_1', True)}
    assert counted(rows) == []


def test_open_legacy_alias_is_counted_once_under_the_qualified_key(tmp_path):
    legacy = {k: v for k, v in card('todo').items() if k not in ('gateway', 'profile')}
    path = replica(tmp_path, [('default/t_1', legacy), ('default/default/t_1', observed(card('todo')))])
    rows = activity._read_remote_links(path, now=NOW)
    assert [(r['key'], r['status'], r['ownership_state']) for r in rows] == [
        ('owner/default/default/t_1', 'todo', 'qualified')]


@pytest.mark.parametrize('legacy_extra', [{'created_at': 2}, {'created_by': 'someone-else'}, {'created_at': None}])
def test_legacy_alias_without_proven_identity_stays_unknown(tmp_path, legacy_extra):
    legacy = {k: v for k, v in card('done', **legacy_extra).items() if k not in ('gateway', 'profile')}
    path = replica(tmp_path, [('default/t_1', legacy), ('default/default/t_1', observed(card('done')))])
    rows = activity._read_remote_links(path, now=NOW)
    legacy_rows = [r for r in rows if r['ownership_state'] == 'unknown']
    assert [(r['key'], r['status'], r['unfinished']) for r in legacy_rows] == [('owner/default/t_1', 'unknown', True)]


def test_legacy_alias_with_two_qualified_twins_is_not_guessed(tmp_path):
    legacy = {k: v for k, v in card('done').items() if k not in ('gateway', 'profile')}
    path = replica(tmp_path, [('default/t_1', legacy), ('default/default/t_1', observed(card('done'))),
                              ('work/default/t_1', observed(card('done', profile='work')))])
    rows = activity._read_remote_links(path, now=NOW)
    assert [r['unfinished'] for r in rows if r['key'] == 'owner/default/t_1'] == [True]


def test_descendants_of_a_folded_root_follow_the_qualified_graph(tmp_path):
    legacy_root = {k: v for k, v in card('done', children=['t_2']).items() if k not in ('gateway', 'profile')}
    path = replica(tmp_path, [
        ('default/t_1', legacy_root),
        ('default/default/t_1', observed(card('done', children=['t_2'], sessions=[]))),
        ('default/default/t_2', observed(card('running', tid='t_2', sessions=[]))),
    ])
    rows = activity._read_remote_links(path, now=NOW)
    assert {(r['key'], r['via'], r['unfinished']) for r in rows} == {
        ('owner/default/default/t_1', 'shepherd', False), ('owner/default/default/t_2', 'descendant', True)}


def test_snapshot_does_not_resurrect_a_folded_or_released_alias(tmp_path, monkeypatch):
    legacy = {k: v for k, v in card('done').items() if k not in ('gateway', 'profile')}
    path = replica(tmp_path, [('default/t_1', legacy), ('default/default/t_1', observed(card('done')))])
    previous = [{'session': 's1', 'key': 'owner/default/t_1', 'id': 't_1', 'status': 'unknown', 'unfinished': True}]
    monkeypatch.setattr(activity, '_hub_db_path', lambda: path)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda cards=None: previous if cards is None else cards)
    monkeypatch.setattr(activity, '_remote_last', None)
    monkeypatch.setattr(activity, '_snapshot_saved', None)
    real = activity._read_remote_links
    monkeypatch.setattr(activity, '_read_remote_links', lambda p: real(p, now=NOW))
    rows = activity._legacy_remote_links()
    assert counted(rows) == []
    assert {r['key'] for r in rows} == {'owner/default/default/t_1'}


def test_scope_heartbeat_between_one_and_two_collector_periods_still_releases(tmp_path):
    """The owner publishes a scope heartbeat about every 180s; a 200s-old one must not flap the release."""
    from hermes_cli import kanban_session_activity as ksa
    assert ksa.SCOPE_HEARTBEAT_FRESH_S >= 400
