"""Conflicting or unsupported retained identity never proves a delayed reference. [daveey]"""
import copy
import json

import pytest

from hermes_cli.kanban_comment_transcript_proof import resolve
from tests.hermes_cli.test_kanban_comment_transcript_controls import evidence


def call(cid, task='target', body='keep the hold', name='kanban_comment', **extra):
    return dict(id=cid, function=dict(name=name, arguments=json.dumps(dict(task_id=task, body=body, **extra))))


def reply(cid, at, **result):
    return dict(role='tool', tool_call_id=cid, content=json.dumps(dict(dict(ok=True, task_id='target'), **result)),
                timestamp=at)


def assistant(calls, at):
    return dict(role='assistant', tool_calls=json.dumps(calls), timestamp=at)


def test_same_array_duplicate_id_for_another_task_refuses():
    rows, event, payload, transcript = evidence()
    transcript[0]['tool_calls'] = json.dumps([call('call'), call('call', task='other-task')])
    assert resolve(rows, event, payload, transcript) == []


@pytest.mark.parametrize('name', ['kanban_comment', 'terminal'])
def test_duplicate_id_in_another_message_refuses(name):
    rows, event, payload, transcript = evidence()
    transcript.insert(1, assistant([call('call', task='other-task', name=name)], 101))
    assert resolve(rows, event, payload, transcript) == []


def test_unrelated_distinct_calls_and_text_replies_still_prove():
    rows, event, payload, transcript = evidence()
    transcript.insert(1, assistant([call('other', name='terminal')], 101))
    transcript.insert(2, dict(role='tool', tool_call_id='other', content='plain text output', timestamp=102))
    assert resolve(rows, event, payload, transcript) == [rows[0]]


@pytest.mark.parametrize('kept', [0, 1])
def test_conflicting_successful_receipts_refuse_whichever_body_survives(kept):
    rows, event, payload, _ = evidence()
    bodies = ['keep the hold', 'conflicting absent body']
    rows[0]['body'] = bodies[kept]
    transcript = [assistant([call('call', body=bodies[0]), call('second-call', body=bodies[1])], 100.2),
                  reply('call', 113.5, comment_id=900001), reply('second-call', 113.6, comment_id=900001)]
    assert resolve(rows, event, payload, transcript) == []


def test_competing_receipt_with_ambiguous_receiver_rows_is_not_discarded():
    rows, event, payload, _ = evidence()
    rows += [dict(rows[0], id=4, uid='a', body='other'), dict(rows[0], id=5, uid='b', body='other')]
    transcript = [assistant([call('call'), call('second-call', body='other')], 100.2),
                  reply('call', 113.5, comment_id=900001), reply('second-call', 113.6, comment_id=900001)]
    assert resolve(rows, event, payload, transcript) == []


@pytest.mark.parametrize('result', [dict(ok=False, comment_id=900001), dict(comment_id=900002),
                                    dict(task_id='other-task', comment_id=900001)])
def test_noncompeting_receipts_do_not_block_the_proof(result):
    rows, event, payload, transcript = evidence()
    transcript.insert(1, assistant([call('second-call', body='unrelated')], 101))
    transcript.insert(2, reply('second-call', 102, **result))
    assert resolve(rows, event, payload, transcript) == [rows[0]]


def test_wrapped_tool_call_is_unsupported_and_stays_missing():
    rows, event, payload, _ = evidence()
    inner = dict(name='kanban_comment', arguments=dict(task_id='target', body='keep the hold'))
    wrapped = dict(id='call', function=dict(name='tool_call', arguments=json.dumps(dict(calls=[inner]))))
    content = json.dumps([dict(ok=True, task_id='target', comment_id=900001)])
    transcript = [assistant([wrapped], 100.2), dict(role='tool', tool_call_id='call', content=content, timestamp=113.5)]
    assert resolve(rows, event, payload, transcript) == []


def test_direct_receipt_from_another_tool_refuses():
    rows, event, payload, transcript = evidence()
    transcript[0]['tool_calls'] = json.dumps([call('call', name='cross_task')])
    assert resolve(rows, event, payload, transcript) == []


@pytest.mark.parametrize('call_board,board,proved', [
    (None, None, True), ('ops', 'ops', True), ('ops', None, False), ('ops', 'default', False)])
def test_call_board_must_match_the_checking_board(call_board, board, proved):
    rows, event, payload, transcript = evidence()
    extra = {} if call_board is None else dict(board=call_board)
    transcript[0]['tool_calls'] = json.dumps([call('call', **extra)])
    before = copy.deepcopy(transcript)
    assert resolve(rows, event, payload, transcript, board=board) == ([rows[0]] if proved else [])
    assert transcript == before
