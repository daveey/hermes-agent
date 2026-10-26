"""Exact owner attestations replace aliases, not unfinished obligations."""
import json
import sqlite3
from copy import deepcopy

import pytest

from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_origin_legacy as legacy


@pytest.fixture
def world(tmp_path):
    binding = {'binding_id': 'binding-one', 'version': 1,
               'owner': {'gateway': 'owner', 'profile': 'work', 'board': 'build', 'task_id': 't_one'},
               'origin': {'gateway': 'origin', 'profile': 'default', 'session': 's_old'},
               'task_record_key': 'work/build/t_one', 'publication_version': 3}
    task = {**binding['owner'], 'id': 't_one', 'status': 'running',
            'origin_bindings': [binding]}
    old = {'id': 't_one', 'board': 'build', 'status': 'done', 'sessions': [
        {'gw': 'origin', 'profile': 'default', 'session': 's_old', 'rel': 'shepherd'}]}
    replica = tmp_path / 'hub.db'
    with sqlite3.connect(replica) as db:
        db.executescript('CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,version INTEGER,data TEXT,updated_at INTEGER);'
                         'CREATE TABLE meta(k TEXT,v TEXT);')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', '100')])
        db.execute("INSERT INTO replica VALUES ('owner','tasks','work/build/t_one',3,?,100000)", (json.dumps(task),))
        db.execute("INSERT INTO replica VALUES ('owner','tasks','build/t_one',2,?,100000)", (json.dumps(old),))
    alias = {'key': 'build/t_one', 'version': 2, 'digest': legacy.record_digest(old),
             'evidence': 'Explicit operator provenance from original owner record'}
    return dict(path=tmp_path / 'ledger.db', replica=replica, binding=binding,
                request_id='request-one', sender='gw:owner', gateway='origin', profile='default',
                resolve_session=lambda sid: 's_tip', legacy_alias=alias)


def test_exact_attestation_is_durable_and_never_releases(world):
    ack = ledger.accept_binding(**world)
    assert ack['legacy_alias'] == world['legacy_alias']
    rows = ledger.project(world['path'], replica=world['replica'], gateway='origin', profile='default', now=100)
    assert len(rows) == 1 and rows[0]['unfinished']
    from hermes_cli.kanban_session_activity import _read_remote_links
    cards = _read_remote_links(world['replica'], now=100)
    old = next(c for c in cards if c['profile'] is None)
    assert legacy.supersedes(old, rows[0])
    assert not legacy.supersedes({**old, 'session': 'unrelated'}, rows[0])
    assert not legacy.supersedes({**old, 'origin_profile': 'work'}, rows[0])
    assert not legacy.supersedes({**old, 'gateway': 'other'}, rows[0])
    assert not legacy.supersedes({**old, 'legacy_record': {**old['legacy_record'], 'version': 4}}, rows[0])
    world['replica'].unlink()
    assert ledger.accept_binding(**world) == ack
    rows = ledger.project(world['path'], replica=None, gateway='origin', profile='default', now=100)
    assert rows[0]['status'] == 'unknown' and rows[0]['unfinished']
    assert legacy.supersedes(old, rows[0])


@pytest.mark.parametrize('field,value', [('key', 'other/t_one'), ('version', True), ('version', 0),
                                        ('digest', 'bad'), ('evidence', ''), ('evidence', None)])
def test_bad_alias_never_creates_receipt(world, field, value):
    world['legacy_alias'][field] = value
    with pytest.raises(ValueError, match='origin_legacy_invalid'):
        ledger.accept_binding(**world)
    assert not world['path'].exists()


@pytest.mark.parametrize('field,value', [('sender', 'gw:other'), ('gateway', 'other'), ('profile', 'work')])
def test_wrong_route_cannot_accept_or_replay(world, field, value):
    ledger.accept_binding(**world)
    world[field] = value
    with pytest.raises(ValueError, match='origin_route_mismatch'):
        ledger.accept_binding(**world)


@pytest.mark.parametrize('change', ['digest', 'version', 'association', 'qualified'])
def test_changed_legacy_publication_is_rejected(world, change):
    with sqlite3.connect(world['replica']) as db:
        raw = db.execute("SELECT data FROM replica WHERE key='build/t_one'").fetchone()[0]
        data = json.loads(raw)
        if change == 'version':
            db.execute("UPDATE replica SET version=4 WHERE key='build/t_one'")
        else:
            if change == 'association':
                data['sessions'][0]['profile'] = 'other'
                world['legacy_alias']['digest'] = legacy.record_digest(data)
            elif change == 'qualified':
                data['profile'] = 'other'
                world['legacy_alias']['digest'] = legacy.record_digest(data)
            else:
                data['title'] = 'changed'
            db.execute("UPDATE replica SET data=? WHERE key='build/t_one'", (json.dumps(data),))
    with pytest.raises(ValueError, match='origin_legacy_'):
        ledger.accept_binding(**world)
    assert ledger.receipt(world['path'], world['request_id']) is None


def test_missing_publication_pending_and_changed_replay_rejected(world):
    original = deepcopy(world['legacy_alias'])
    world['legacy_alias']['version'] = 9
    assert ledger.accept_binding(**world)['status'] == 'pending'
    world['legacy_alias'] = original
    ledger.accept_binding(**world)
    world['legacy_alias'] = None
    with pytest.raises(ValueError, match='origin_binding_conflict'):
        ledger.accept_binding(**world)
    world['legacy_alias'] = {**original, 'evidence': 'different claim'}
    with pytest.raises(ValueError, match='origin_binding_conflict'):
        ledger.accept_binding(**world)


def test_competing_profile_attestation_is_rejected(world):
    ledger.accept_binding(**world)
    other = deepcopy(world['binding'])
    other['owner']['profile'] = 'other'
    other['task_record_key'] = 'other/build/t_one'
    task = {**other['owner'], 'id': 't_one', 'status': 'running', 'origin_bindings': [other]}
    with sqlite3.connect(world['replica']) as db:
        db.execute("INSERT INTO replica VALUES ('owner','tasks',?,3,?,100000)",
                   (other['task_record_key'], json.dumps(task)))
    with pytest.raises(ValueError, match='origin_legacy_conflict'):
        ledger.accept_binding(**{**world, 'binding': other, 'request_id': 'request-two'})
    assert ledger.receipt(world['path'], 'request-two') is None


def test_receiver_and_session_projection_replace_only_attested_alias(world, tmp_path, monkeypatch):
    from hermes_cli import kanban_origin_adapter as adapter, kanban_session_activity as activity, profiles
    import hermes_constants
    home = tmp_path / 'home'
    home.mkdir()
    world['path'] = home / 'kanban/origin-ledger.db'
    cfg = tmp_path / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'origin'}))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    monkeypatch.setenv('HERMES_HUB_STATE', str(tmp_path))
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(adapter, '_resolve_session', lambda path, sid: 's_tip')
    request = {'id': 'request-one', 'from': 'gw:owner', 'to_gw': 'origin',
               'coll': 'sessions', 'key': 'default/s_old', 'action': 'origin-bind',
               'args': {'binding': world['binding'], 'legacy_alias': world['legacy_alias']}}
    assert adapter.receive(request)['legacy_alias'] == world['legacy_alias']
    monkeypatch.setattr(activity, '_hub_db_path', lambda: world['replica'])
    monkeypatch.setattr(activity, '_legacy_remote_links', lambda: activity._read_remote_links(world['replica'], now=100))
    rows = activity._remote_links()
    assert len(rows) == 1 and rows[0]['profile'] == 'work' and rows[0]['unfinished']
    # A new version of the legacy record is a new obligation, not covered by old proof.
    with sqlite3.connect(world['replica']) as db:
        db.execute("UPDATE replica SET version=4 WHERE key='build/t_one'")
    rows = activity._remote_links()
    assert len(rows) == 2 and any(r['profile'] is None for r in rows)


def test_existing_binding_revision_is_preserved_when_task_has_advanced(world):
    binding_only = {**world, 'legacy_alias': None, 'request_id': 'original-request'}
    ledger.accept_binding(**binding_only)
    with sqlite3.connect(world['replica']) as db:
        db.execute("UPDATE replica SET version=10 WHERE key='work/build/t_one'")
    ack = ledger.accept_binding(**world)
    assert ack['binding_hash'] == ledger.binding_hash(world['binding'])
    assert ack['legacy_alias'] == world['legacy_alias']
