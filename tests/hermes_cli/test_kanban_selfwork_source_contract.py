"""Discriminate producer-field guards through real, read-only local custody.

Synthetic SQLite fixtures, not claims about historical production rows. Keep
adjacent receipt/revision constraints coherent so one cannot mask another.
"""
import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_card_answer_runtime as runtime
from hermes_cli import install_identity
from hermes_cli import kanban_selfwork_exception as sx
from hermes_cli import kanban_selfwork_local_source as source


@pytest.fixture
def producer(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, '_profile', lambda: ('default', tmp_path))
    monkeypatch.setattr(install_identity, 'get_install_id', lambda: 'fixture-install')
    owner = dict(gatewayId='install:fixture-install', profile='default', storedSessionId='creator')
    frozen = dict(requestId='request', revision=1, owner=owner, choices=[
        dict(effect='answer', id='card-0', label='Unblock'),
        dict(effect='answer', id='card-1', label='Archive')])
    op = dict(operationId='operation', owner=copy.deepcopy(owner), requestId='request', revision=1,
              recordedAt='2026-01-01T00:00:00Z', dispatchState='delivered', idempotencyKey='key',
              intent=dict(request=copy.deepcopy(frozen), effect='answer', revisionText='Unblock',
                          executionAuthorized=False, permissionGrants=[]),
              decision=dict(requestId='request', expectedRevision=1, choiceId='card-0',
                            revisionText='Unblock', idempotencyKey='key'))
    snapshot = dict(task=sx.TARGET.owner_task, session='creator', generation=1, ask={'action': 'test'})
    path = tmp_path / 'user-steps/actions.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE operations(namespace TEXT, session TEXT, id TEXT, body TEXT);
            CREATE TABLE kanban_projections(namespace TEXT, session TEXT, request_id TEXT,
                board TEXT, task TEXT, request TEXT, snapshot TEXT);
            CREATE TABLE requests(namespace TEXT, session TEXT, id TEXT, revision INTEGER,
                state TEXT, resolution TEXT);
            CREATE TABLE revisions(namespace TEXT, id TEXT, revision INTEGER, body TEXT);
        ''')
    return path, op, frozen, snapshot


def seed(producer):
    path, op, frozen, snapshot = producer
    namespace = '["install:fixture-install","default"]'
    receipt = dict(destination='kanban_ask', board='default', task=sx.TARGET.owner_task,
                   operationId='operation', value='unblock', executionStarted=False,
                   fingerprint=source.digest([op['intent'], op['decision']]))
    op['receipt'] = receipt
    resolution = dict(choiceId='card-0', operationId='operation', revision=op['revision'],
                      recordedAt=op['recordedAt'])
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO operations VALUES(?,?,?,?)',
                     (namespace, 'creator', 'operation', json.dumps(op)))
        conn.execute('INSERT INTO kanban_projections VALUES(?,?,?,?,?,?,?)',
                     (namespace, 'creator', 'request', 'default', sx.TARGET.owner_task,
                      json.dumps(frozen), json.dumps(snapshot)))
        conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
                     (namespace, 'creator', 'request', op['revision'], 'open', json.dumps(resolution)))
        conn.execute('INSERT INTO revisions VALUES(?,?,?,?)',
                     (namespace, 'request', op['revision'], json.dumps(frozen)))


def test_custody_positive_and_read_only(producer):
    seed(producer)
    before = producer[0].read_bytes()
    with source.custody('operation', delivered=True) as evidence:
        assert evidence['operation'] == producer[1]
        assert evidence['snapshot'] == producer[3]
        assert evidence['source']['session'] == 'creator'
    assert producer[0].read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('owner', {}), ('operationId', 'other'), ('recordedAt', 1), ('recordedAt', ''),
    ('dispatchState', 'unknown'), ('retired', True),
])
def test_operation_field_refusals(producer, field, value):
    producer[1][field] = value
    seed(producer)
    with pytest.raises(sx.Refused, match='^local_operation_mismatch$'):
        with source.custody('operation', delivered=False):
            pytest.fail('invalid producer accepted')


@pytest.mark.parametrize('field,value', [
    ('task', 'other'), ('session', 'other'), ('generation', True), ('ask', []),
    ('ask', {'choices': ['custom']}), ('ask', {'release_target': 'other'}),
])
def test_snapshot_field_refusals(producer, field, value):
    producer[3][field] = value
    seed(producer)
    with pytest.raises(sx.Refused, match='^local_projection_target_mismatch$'):
        with source.custody('operation', delivered=True):
            pytest.fail('invalid projection accepted')


@pytest.mark.parametrize('section,field,value', [
    ('intent', 'request', {}), ('intent', 'effect', 'execute'),
    ('intent', 'revisionText', 'Archive'), ('intent', 'executionAuthorized', True),
    ('intent', 'permissionGrants', ['grant']), ('decision', 'expectedRevision', 2),
    ('decision', 'requestId', 'other'), ('decision', 'choiceId', 'card-1'),
    ('decision', 'revisionText', 'Archive'), ('decision', 'idempotencyKey', 'other'),
    ('op', 'provenance', {'remote': True}),
])
def test_intent_field_refusals(producer, section, field, value):
    op = producer[1]
    target = op if section == 'op' else op[section]
    target[field] = value
    seed(producer)  # recomputes fingerprint: not a receipt mismatch test
    with pytest.raises(sx.Refused, match='^local_operation_intent_mismatch$'):
        with source.custody('operation', delivered=True):
            pytest.fail('invalid intent accepted')


@pytest.mark.parametrize('field,value', [
    ('owner', {}), ('choices', []), ('revision', True), ('revision', 0), ('requestId', 'other'),
])
def test_frozen_field_refusals(producer, field, value):
    _, op, frozen, _ = producer
    frozen[field] = value
    op['intent']['request'] = copy.deepcopy(frozen)
    if field == 'revision':
        op['revision'] = value
        op['decision']['expectedRevision'] = value
    if field == 'requestId':
        op['decision']['requestId'] = value
    seed(producer)
    with pytest.raises(sx.Refused, match='^local_operation_intent_mismatch$'):
        with source.custody('operation', delivered=True):
            pytest.fail('invalid frozen request accepted')


@pytest.mark.parametrize('table,reason', [
    ('operations', 'local_operation_missing_or_ambiguous'),
    ('kanban_projections', 'local_projection_missing_or_ambiguous'),
    ('requests', 'local_request_revision_changed'),
    ('revisions', 'local_request_revision_changed'),
])
def test_missing_source_rows(producer, table, reason):
    seed(producer)
    with sqlite3.connect(producer[0]) as conn:
        conn.execute('DELETE FROM ' + table)
    before = producer[0].read_bytes()
    with pytest.raises(sx.Refused, match='^' + reason + '$'):
        with source.custody('operation', delivered=True):
            pytest.fail('missing source accepted')
    assert producer[0].read_bytes() == before


@pytest.mark.parametrize('oid', [None, '', '../operation', 'x' * 201])
def test_invalid_operation_lookup(producer, oid):
    seed(producer)
    with pytest.raises(sx.Refused, match='^local_operation_id_invalid$'):
        with source.custody(oid):
            pytest.fail('invalid lookup accepted')


def test_missing_install(producer, monkeypatch):
    seed(producer)
    monkeypatch.setattr(install_identity, 'get_install_id', lambda: '')
    with pytest.raises(sx.Refused, match='^local_install_unavailable$'):
        with source.custody('operation'):
            pytest.fail('unidentified installation accepted')


@pytest.mark.parametrize('table,reason', [
    ('operations', 'local_operation_missing_or_ambiguous'),
    ('kanban_projections', 'local_projection_missing_or_ambiguous'),
])
def test_ambiguous_source_rows(producer, table, reason):
    seed(producer)
    with sqlite3.connect(producer[0]) as conn:
        conn.execute('INSERT INTO ' + table + ' SELECT * FROM ' + table)
    with pytest.raises(sx.Refused, match='^' + reason + '$'):
        with source.custody('operation'):
            pytest.fail('ambiguous source accepted')


def test_empty_source_session(producer):
    _, op, frozen, snapshot = producer
    op['owner']['storedSessionId'] = ''
    frozen['owner']['storedSessionId'] = ''
    op['intent']['request'] = copy.deepcopy(frozen)
    snapshot['session'] = ''
    seed(producer)
    with sqlite3.connect(producer[0]) as conn:
        for table in ('operations', 'kanban_projections', 'requests'):
            conn.execute('UPDATE ' + table + " SET session=''")
    with pytest.raises(sx.Refused, match='^local_operation_mismatch$'):
        with source.custody('operation'):
            pytest.fail('empty session accepted')


def test_operation_revision_not_frozen_revision(producer):
    producer[1]['revision'] = 2
    seed(producer)
    with pytest.raises(sx.Refused, match='^local_operation_intent_mismatch$'):
        with source.custody('operation'):
            pytest.fail('changed revision accepted')


@pytest.mark.parametrize('damage,reason', [
    ('refused', 'local_operation_missing_or_ambiguous'),
    ('schema', 'local_source_unavailable'),
    ('malformed', 'local_source_malformed'),
])
def test_refusal_closes_source(producer, monkeypatch, damage, reason):
    seed(producer)
    with sqlite3.connect(producer[0]) as conn:
        if damage == 'refused':
            conn.execute('DELETE FROM operations')
        elif damage == 'schema':
            conn.execute('DROP TABLE operations')
        else:
            conn.execute("UPDATE operations SET body='[]'")
    closed = []
    original = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def connect(*args, **kwargs):
        return original(*args, factory=ObservedConnection, **kwargs)

    monkeypatch.setattr(source.sqlite3, 'connect', connect)
    with pytest.raises(sx.Refused, match='^' + reason + '$'):
        with source.custody('operation'):
            pytest.fail('damaged source accepted')
    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        closed[0].execute('SELECT 1')


def test_missing_source_refuses_without_creating_file(producer):
    producer[0].unlink()
    with pytest.raises(sx.Refused, match='^local_source_unavailable$'):
        with source.custody('operation'):
            pytest.fail('missing store accepted')
    assert not producer[0].exists()


@pytest.mark.parametrize('message,busy', [('database is locked', True), ('database is busy', True),
                                        ('unavailable', False)])
def test_sqlite_open_failure_classification(producer, monkeypatch, message, busy):
    # Transport fault seam, not an assertion that this host experienced a lock.
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(message)
    monkeypatch.setattr(source.sqlite3, 'connect', fail)
    error, reason = (OSError, 'local_source_busy') if busy else (sx.Refused, 'local_source_unavailable')
    with pytest.raises(error, match='^' + reason + '$'):
        with source.custody('operation'):
            pytest.fail('failed source accepted')


def test_null_resolution_has_revision_refusal(producer):
    seed(producer)
    with sqlite3.connect(producer[0]) as conn:
        conn.execute('UPDATE requests SET resolution=NULL')
    with pytest.raises(sx.Refused, match='^local_request_revision_changed$'):
        with source.custody('operation'):
            pytest.fail('unresolved decision accepted')


def test_open_value_error_preserves_normalized_refusal(producer, monkeypatch):
    # A pre-connect adapter error must not be masked by closing a null handle.
    def fail(*args, **kwargs):
        raise ValueError('malformed connection input')
    monkeypatch.setattr(source.sqlite3, 'connect', fail)
    with pytest.raises(sx.Refused, match='^local_source_malformed$'):
        with source.custody('operation'):
            pytest.fail('malformed source accepted')
