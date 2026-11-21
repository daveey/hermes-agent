"""Courier identity and source-operation proof fencing (no live Board writes)."""
import copy

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_proof as proof


def records():
    answer = dict(ownerGateway='owner', ownerProfile='default', board='default', task='t_example',
                  generation='a' * 64, choice='plan-a', operationId='operation-one')
    source = dict(gateway='origin', profile='work', session='chat-one')
    original = dict(id='original', coll='tasks', action='card-answer', key='default/default/t_example',
                    to_gw='owner', args=dict(answer=answer, source=source), **{'from': 'gw:origin'})
    expected = dict(answer=answer, source=source, requestId='original')
    receipt = dict(id='proof', coll='sessions', action='card-answer-proof', key='work/chat-one',
                   to_gw='origin', args=copy.deepcopy(expected), state='done',
                   result=dict(copy.deepcopy(expected), gateway='origin', recorded=True), **{'from': 'gw:owner'})
    return answer, {'original': original, 'proof': receipt}


def verify(answer, rows):
    calls = []
    def read(rid):
        calls.append(rid)
        return rows.get(rid)
    result = proof.verify(answer, request_id='original', proof_id='proof', owner_gateway='owner', read_request=read)
    assert calls == ['original', 'proof']
    return result


def test_exact_authenticated_records_accept():
    answer, rows = records()
    assert verify(answer, rows) == rows['proof']['result']


@pytest.mark.parametrize('record,field,value', [
    ('original', 'id', 'other'), ('original', 'coll', 'sessions'),
    ('original', 'action', 'unblock'), ('original', 'from', 'gw:attacker'),
    ('original', 'to_gw', 'wrong'), ('original', 'key', 'wrong/default/t_example'),
    ('proof', 'id', 'other'), ('proof', 'coll', 'tasks'),
    ('proof', 'action', 'card-answer'), ('proof', 'from', 'gw:origin'),
    ('proof', 'to_gw', 'owner'), ('proof', 'key', 'default/chat-one'),
    ('proof', 'state', 'pending'), ('proof', 'state', 'rejected'),
    ('proof', 'result', {'recorded': True}),
])
def test_identity_mutations_refuse(record, field, value):
    answer, rows = records()
    rows[record][field] = value
    with pytest.raises(ca.AnswerRefused):
        verify(answer, rows)


@pytest.mark.parametrize('field', ['ownerGateway', 'ownerProfile', 'board', 'task', 'generation', 'choice', 'operationId'])
@pytest.mark.parametrize('where', ['original', 'proof-args', 'result'])
def test_every_answer_coordinate_is_bound(field, where):
    answer, rows = records()
    rows = copy.deepcopy(rows)
    target = (rows['original']['args']['answer'] if where == 'original' else
              rows['proof']['args']['answer'] if where == 'proof-args' else rows['proof']['result']['answer'])
    target[field] = 'changed'
    with pytest.raises(ca.AnswerRefused):
        verify(answer, rows)


@pytest.mark.parametrize('where', ['args', 'result'])
@pytest.mark.parametrize('field', ['gateway', 'profile', 'session'])
def test_source_namespace_and_session_are_bound(where, field):
    answer, rows = records()
    rows['proof'][where]['source'][field] = 'changed'
    with pytest.raises(ca.AnswerRefused):
        verify(answer, rows)


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_recorded_requires_true_boolean(value):
    answer, rows = records()
    rows['proof']['result']['recorded'] = value
    with pytest.raises(ca.AnswerRefused):
        verify(answer, rows)


def test_missing_and_malformed_records_fail_closed():
    answer, rows = records()
    del rows['proof']
    with pytest.raises(ca.AnswerRefused, match='source_proof_unavailable'):
        verify(answer, rows)
    answer, rows = records()
    rows['original']['args']['source']['session'] = 'nested/session'
    with pytest.raises(ca.AnswerRefused, match='source_request_invalid'):
        verify(answer, rows)
