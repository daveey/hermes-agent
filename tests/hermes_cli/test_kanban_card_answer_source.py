"""Read-only SQLite proof tests using disposable action-store-shaped rows."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_source as source


def fixture(tmp_path):
    path = tmp_path / 'actions.sqlite3'
    answer = dict(ownerGateway='owner', ownerProfile='default', board='default', task='t_example',
                  generation='a' * 64, choice='plan-a', operationId='operation-one')
    origin = dict(gateway='origin', profile='work', session='chat-one')
    owner = dict(gatewayId='origin', profile='work', storedSessionId='chat-one')
    request = dict(requestId='request-one', revision=1, choices=[dict(id='a', effect='answer', label='Apply A')])
    op = dict(owner=owner, operationId='operation-one', recordedAt='2026-01-01T00:00:00+00:00',
              requestId='request-one', revision=1, dispatchState='pending',
              decision=dict(requestId='request-one', expectedRevision=1, choiceId='a'),
              intent=dict(request=request, effect='answer', revisionText='Apply A',
                          executionAuthorized=False, permissionGrants=[]))
    remote = dict(version=1, target={k: v for k, v in answer.items() if k not in ('choice', 'operationId')},
                  choiceValues={'a': 'plan-a'})
    ns = json.dumps(['origin', 'work'], separators=(',', ':'))
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE operations(namespace TEXT,session TEXT,id TEXT,body TEXT)')
        conn.execute('CREATE TABLE kanban_projections(namespace TEXT,session TEXT,request_id TEXT,board TEXT,task TEXT,snapshot TEXT,request TEXT)')
        conn.execute('INSERT INTO operations VALUES (?,?,?,?)', (ns, 'chat-one', 'operation-one', json.dumps(op)))
        conn.execute('INSERT INTO kanban_projections VALUES (?,?,?,?,?,?,?)',
                     (ns, 'chat-one', 'request-one', 'default', 't_example', json.dumps(dict(remote=remote)), json.dumps(request)))
    return path, answer, origin, op, remote


def read(path, answer, origin):
    return source.recorded(path, source=origin, owner_gateway='owner', request_id='original', answer=answer)


def test_exact_operation_and_binding_read_only(tmp_path):
    path, answer, origin, _, _ = fixture(tmp_path)
    before = path.read_bytes()
    result = read(path, answer, origin)
    assert result == dict(answer=answer, source=origin, requestId='original', gateway='origin', recorded=True)
    assert path.read_bytes() == before
    assert not (tmp_path / 'actions.sqlite3-wal').exists()
    assert not (tmp_path / 'actions.sqlite3-shm').exists()


@pytest.mark.parametrize('field', ['gateway', 'profile', 'session'])
def test_no_cross_namespace_search(tmp_path, field):
    path, answer, origin, _, _ = fixture(tmp_path)
    origin[field] = 'another'
    with pytest.raises(ca.AnswerRefused, match='source_operation_missing'):
        read(path, answer, origin)


@pytest.mark.parametrize('field,value', [('owner', {}), ('operationId', 'other'), ('dispatchState', 'cancelled'),
                                        ('recordedAt', ''), ('revision', 2)])
def test_changed_operation_refuses(tmp_path, field, value):
    path, answer, origin, op, _ = fixture(tmp_path)
    op[field] = value
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE operations SET body=?', (json.dumps(op),))
    with pytest.raises(ca.AnswerRefused):
        read(path, answer, origin)


@pytest.mark.parametrize('field,value', [('effect', 'approve'), ('revisionText', 'other'),
                                        ('executionAuthorized', True), ('permissionGrants', ['network'])])
def test_informational_only_no_execution_grants(tmp_path, field, value):
    path, answer, origin, op, _ = fixture(tmp_path)
    op['intent'][field] = value
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE operations SET body=?', (json.dumps(op),))
    with pytest.raises(ca.AnswerRefused, match='source_choice_mismatch'):
        read(path, answer, origin)


@pytest.mark.parametrize('field', ['ownerGateway', 'ownerProfile', 'board', 'task', 'generation'])
def test_frozen_target_cannot_be_redirected(tmp_path, field):
    path, answer, origin, _, remote = fixture(tmp_path)
    remote['target'][field] = 'changed'
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE kanban_projections SET snapshot=?', (json.dumps(dict(remote=remote)),))
    with pytest.raises(ca.AnswerRefused, match='source_target_mismatch'):
        read(path, answer, origin)


def test_unbound_operation_is_not_authority(tmp_path):
    path, answer, origin, _, _ = fixture(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM kanban_projections')
    with pytest.raises(ca.AnswerRefused, match='source_binding_missing'):
        read(path, answer, origin)


def test_missing_store_never_created(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        read(path, {}, dict(gateway='origin', profile='work', session='chat-one'))
    assert not path.exists()
