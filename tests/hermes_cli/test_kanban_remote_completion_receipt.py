"""Synthetic owner publications through the real remote inventory producers."""
import hashlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_origin_ledger as fixtures
from tests.hermes_state.test_completion_receipt import receipt

world = fixtures.world


def publication(binding):
    owner = binding['owner']
    return {**owner, 'id': owner['task_id'], 'status': 'done', 'title': 'Assessment',
            'origin_bindings': [binding],
            'sessions': [{'gw': 'origin', 'profile': 'default', 'session': 's_current', 'rel': 'shepherd'}],
            'completion_receipt': receipt('rejected'),
            'completion_receipt_source': {
                'kind': 'task_run', 'ref': '@task:build@owner/t_one',
                'event_id': 7, 'event_uid': 'event-original', 'run_id': 4,
                'run_uid': 'run-original', 'completed_at': 990,
                'attached_from_workspace': {'https://example.com/review/1': '/attachments/review'}},
            }


def attest(task, *, verified=True):
    task['origin_observation'] = {'schema': 1, 'verified': verified, 'observed_at': 1000,
                                  'task_hash': hashlib.sha256(ledger._json(task).encode()).hexdigest()}


def project(world, monkeypatch, producer, task, *, now=1001):
    path, replica, binding = world
    with sqlite3.connect(replica) as db:
        db.execute('ALTER TABLE replica ADD COLUMN updated_at INTEGER DEFAULT 1000000')
        db.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        db.executemany('INSERT INTO meta VALUES (?, ?)', [('last_ok', '1000'), ('gateway', 'origin')])
        db.execute("INSERT INTO replica (gw,coll,key,version,data) VALUES ('owner','tasks',?,3,?)",
                   (binding['task_record_key'], json.dumps(task)))
        info = {'gateway': 'owner', 'task_scopes': [{'profile': 'work', 'board': 'build',
                'state': 'ok', 'observed_at': 1000}]}
        db.execute("INSERT INTO replica (gw,coll,key,version,data) VALUES ('owner','gateway','info',3,?)", (json.dumps(info),))
    if producer == 'ledger':
        fixtures.accept(world)
        return ledger.project(path, replica=replica, gateway='origin', profile='default', now=now)[0]
    monkeypatch.setattr(activity, '_self_gateway', lambda _: 'origin')
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    return activity._read_remote_links(replica, now=now)[0]


@pytest.mark.parametrize('producer', ['ledger', 'activity'])
def test_remote_producer_preserves_source_receipt_without_upgrading_evidence(world, monkeypatch, producer):
    task = publication(world[2])
    attest(task)
    row = project(world, monkeypatch, producer, task)
    assert row.get('completion_receipt') == task['completion_receipt']
    assert row.get('completion_receipt_source') == task['completion_receipt_source']
    assert row['stale'] is False
    assert row['unfinished'] is False
    assert row['completion_receipt']['items'][0]['evidence'][0]['verification'] == 'reported'


@pytest.mark.parametrize('field', ['gateway', 'profile', 'board'])
def test_activity_owner_collision_cannot_borrow_another_qualified_receipt(world, monkeypatch, field):
    task = publication(world[2])
    task[field] = 'collision'
    attest(task)
    row = project(world, monkeypatch, 'activity', task)
    assert 'completion_receipt' not in row
    assert 'completion_receipt_source' not in row
    assert row['unfinished'] is True
    assert row['status'] == 'unknown'


@pytest.mark.parametrize('field', ['gateway', 'profile', 'board'])
def test_ledger_owner_collision_cannot_borrow_another_qualified_receipt(world, monkeypatch, field):
    task = publication(world[2])
    # Accept a real owner publication first, then replace its inventory row.
    valid = publication(world[2])
    attest(valid)
    row = project(world, monkeypatch, 'ledger', valid)
    assert 'completion_receipt' in row
    task[field] = 'collision'
    task['completion_receipt'] = receipt('delivered')
    task['completion_receipt_source']['event_uid'] = 'collision-event'
    attest(task)
    with sqlite3.connect(world[1]) as db:
        db.execute("UPDATE replica SET version=4,data=? WHERE coll='tasks'", (json.dumps(task),))
    row = ledger.project(world[0], replica=world[1], gateway='origin', profile='default', now=1002)[0]
    # The durable previously admitted historical receipt survives. A collision
    # must neither borrow the new receipt nor preserve current settlement.
    assert row['completion_receipt'] == valid['completion_receipt']
    assert row['completion_receipt_source'] == valid['completion_receipt_source']
    assert row['unfinished'] is True


@pytest.mark.parametrize('verified', [True, False])
def test_self_owner_ledger_projects_local_receipt_without_upgrading_completion(world, monkeypatch, verified):
    from hermes_cli import kanban_origin_local
    binding = world[2]
    binding['owner'].update(gateway='origin', profile='default')
    binding['task_record_key'] = 'default/build/t_one'
    task = publication(binding)
    task['completion_receipt_source']['ref'] = '@task:build@origin/t_one'
    attest(task, verified=verified)
    fixtures.publish(world[1], binding)
    fixtures.accept(world, sender='gw:origin')
    monkeypatch.setattr(kanban_origin_local, 'observe_local_binding', lambda *a, **kw: task)
    row = ledger.project(world[0], replica=None, gateway='origin', profile='default', now=1001)[0]
    assert row['completion_receipt_source'] == task['completion_receipt_source']
    assert row['completion_receipt'] == task['completion_receipt']
    assert row['unfinished'] is (not verified)
    assert row['status'] == ('done' if verified else 'unknown')


@pytest.mark.parametrize('producer', ['ledger', 'activity'])
def test_stale_receipt_is_historical_not_current_delivery(world, monkeypatch, producer):
    task = publication(world[2])
    task['completion_receipt'] = receipt('delivered')
    attest(task)
    row = project(world, monkeypatch, producer, task, now=2000)
    assert row.get('completion_receipt') == task['completion_receipt']
    assert row.get('completion_receipt_source') == task['completion_receipt_source']
    assert row['stale'] is True
    assert row['unfinished'] is True
    assert row['status'] == 'unknown'
    assert row.get('released', False) is False


@pytest.mark.parametrize('producer', ['ledger', 'activity'])
@pytest.mark.parametrize('fault', ['legacy', 'unattested', 'tampered', 'source_gateway',
                                  'source_board', 'source_task', 'source_kind', 'malformed',
                                  'missing_source', 'missing_event', 'invalid_run'])
def test_remote_producers_do_not_invent_or_misattribute_receipts(world, monkeypatch, producer, fault):
    task = publication(world[2])
    if fault == 'legacy':
        del task['completion_receipt']
        del task['completion_receipt_source']
    elif fault.startswith('source_'):
        field = fault.removeprefix('source_')
        task['completion_receipt_source']['kind' if field == 'kind' else 'ref'] = {
            'gateway': '@task:build@collision/t_one', 'board': '@task:other@owner/t_one',
            'task': '@task:build@owner/t_collision', 'kind': 'session_declaration'}[field]
    elif fault == 'missing_source':
        del task['completion_receipt_source']
    elif fault == 'missing_event':
        del task['completion_receipt_source']['event_id']
    elif fault == 'invalid_run':
        task['completion_receipt_source']['run_id'] = True
    elif fault == 'malformed':
        task['completion_receipt']['items'] = []
    if fault != 'unattested':
        attest(task)
    if fault == 'tampered':
        task['completion_receipt']['items'][0]['disposition'] = 'delivered'
    row = project(world, monkeypatch, producer, task)
    assert 'completion_receipt' not in row
    assert 'completion_receipt_source' not in row
    if fault in ('unattested', 'tampered'):
        assert row['unfinished'] is True
        assert row['status'] == 'unknown'


@pytest.mark.parametrize('producer', ['ledger', 'activity'])
@pytest.mark.parametrize('field,value', [
    ('extra', 'X' * 100000), ('event_uid', {'payload': 'X' * 50000}),
    ('run_uid', 'X' * 50000), ('event_uid', ''), ('completed_at', '990'),
    ('completed_at', float('inf')), ('completed_at', -1), ('completed_at', True),
    ('attached_from_workspace', {'unknown:ref': '/attachment'}),
    ('attached_from_workspace', {'https://example.com/review/1': 'X' * 50000}),
])
def test_attested_malformed_provenance_is_not_transported(world, monkeypatch, producer, field, value):
    task = publication(world[2])
    task['completion_receipt_source'][field] = value
    attest(task)
    row = project(world, monkeypatch, producer, task)
    assert 'completion_receipt_source' not in row
    assert 'completion_receipt' not in row
    assert row['unfinished'] is False  # provenance refusal must not change workflow gates


@pytest.mark.parametrize('producer', ['ledger', 'activity'])
def test_receipt_does_not_override_unverified_owner_completion(world, monkeypatch, producer):
    task = publication(world[2])
    task['completion_receipt'] = receipt('delivered')
    attest(task, verified=False)
    row = project(world, monkeypatch, producer, task)
    assert row['unfinished'] is True
    assert row['status'] == 'unknown'
    assert row.get('released', False) is False
    assert row['completion_receipt']['items'][0]['evidence'][0]['verification'] == 'reported'
