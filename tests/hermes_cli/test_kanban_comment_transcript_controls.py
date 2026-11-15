"""Fail-closed transcript corroboration controls. [daveey]"""
import copy
import json

import pytest

from hermes_cli.kanban_comment_transcript_proof import resolve


def evidence():
    row = dict(id=3, uid='receiver', body='keep the hold', author='default', created_at=100)
    event = dict(id=9, task_id='target', created_at=113)
    payload = dict(comment_id=900001, author='default', writer_session='writer')
    call = dict(id='call', function=dict(name='kanban_comment', arguments=dict(task_id='target', body=row['body'])))
    transcript = [dict(role='assistant', tool_calls=json.dumps([call]), timestamp=100.2),
                  dict(role='tool', tool_call_id='call', content=json.dumps(dict(ok=True, task_id='target', comment_id=900001)), timestamp=113.5)]
    return [row], event, payload, transcript


@pytest.mark.parametrize('target,key,value', [
    ('row', 'body', 'another body'), ('row', 'author', 'other'),
    ('event', 'task_id', 'other'), ('payload', 'comment_id', 3),
    ('payload', 'comment_id', True), ('reply', 'content', '{"ok":false}'),
    ('reply', 'tool_call_id', 'other'), ('event', 'created_at', 99),
    ('event', 'created_at', 114), ('row', 'created_at', 99),
    ('row', 'created_at', 114), ('call', 'tool_calls', '{broken'),
])
def test_unproven_association_stays_missing(target, key, value):
    rows, event, payload, transcript = evidence()
    targets = dict(row=rows[0], event=event, payload=payload, reply=transcript[1], call=transcript[0])
    targets[target][key] = value
    assert resolve(rows, event, payload, transcript) == []


@pytest.mark.parametrize('duplicate', ['row', 'reply', 'call'])
def test_ambiguous_evidence_stays_missing(duplicate):
    rows, event, payload, transcript = evidence()
    target, value = dict(row=(rows, dict(rows[0], id=4, uid='second')),
                         reply=(transcript, transcript[1]), call=(transcript, transcript[0]))[duplicate]
    target.append(copy.deepcopy(value))
    assert resolve(rows, event, payload, transcript) == []


def test_receiver_integer_collision_does_not_choose_the_row():
    rows, event, payload, transcript = evidence()
    rows.append(dict(rows[0], id=900001, uid='collision', body='do not confuse me'))
    before = copy.deepcopy((rows, event, payload, transcript))
    assert resolve(rows, event, payload, transcript) == [rows[0]]
    assert (rows, event, payload, transcript) == before
    assert resolve(rows, event, payload, []) == []
