"""Exact owner writes, duplicate-delivery races and module error envelopes."""
import json
import sqlite3
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_runtime as runtime
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_card_answer as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('suffix', [' ', '\n', '/', '\t'])
def test_invalid_operation_id_never_writes(kanban_home, suffix):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        before = ca.snapshot(conn, answer['task'])
        answer['operationId'] += suffix
        with pytest.raises(ca.AnswerRefused, match='invalid_answer'):
            helpers.apply(conn, answer)
        assert ca.snapshot(conn, answer['task']) == before
        assert helpers.comments(conn, answer['task']) == []


@pytest.mark.parametrize('mode', ['choice', 'comment'])
def test_duplicate_between_preflight_and_write_lock_returns_receipt(kanban_home, monkeypatch, mode):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        if mode == 'comment':
            answer.update(mode='comment', choice='Need more context')
        write_txn = kb.write_txn
        winner = []
        @contextmanager
        def competing_write(c, *args, **kwargs):
            # Force the other connection's commit after this delivery's snapshot
            # and preflight receipt read, but before it takes the write lock.
            monkeypatch.setattr(kb, 'write_txn', write_txn)
            with kbc.connect_closing() as other:
                winner.append(helpers.apply(other, answer))
            with write_txn(c, *args, **kwargs):
                yield
        monkeypatch.setattr(kb, 'write_txn', competing_write)
        loser = helpers.apply(conn, answer)
        assert loser == winner[0]
        assert len(helpers.comments(conn, answer['task'])) == 1
        assert kb.get_task(conn, answer['task']).status == ('blocked' if mode == 'comment' else 'ready')


@pytest.mark.parametrize('error', [sqlite3.DatabaseError, sqlite3.IntegrityError, AttributeError])
@pytest.mark.parametrize('argv', [['owner', 'request', 'proof'], ['source', 'proof']])
def test_cli_internal_error_is_one_json_object(monkeypatch, capsys, error, argv):
    def boom(*args):
        raise error('private transport detail')
    monkeypatch.setattr(runtime, argv[0], boom)
    assert runtime.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == dict(ok=False, error='internal_error', type=error.__name__)


@pytest.mark.parametrize('status', ['ready', 'running', 'done', 'archived'])
def test_snapshot_excludes_nonasks(kanban_home, status):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, answer['task']))
        assert ca.snapshot(conn, answer['task']) is None


@pytest.mark.parametrize('field,value', [('recorded', False), ('recorded', 1), ('answer', {}),
    ('gateway', ''), ('gateway', None), ('gateway', 42)])
def test_proof_fields_independently_fenced(kanban_home, field, value):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        proof = dict(helpers.verified(dict(answer)), **{field: value})
        with pytest.raises(ca.AnswerRefused, match='unverified_user_decision'):
            helpers.apply(conn, answer, lambda _: proof)
        assert not helpers.comments(conn, answer['task'])


def test_explicit_archive_value_is_an_ordinary_choice(kanban_home):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=json_set(human_request,'$.choices',json(?)) WHERE id=?",
                (json.dumps([dict(label='Continue', value='archive')]), answer['task']))
        answer.update(choice='archive', generation=ca.snapshot(conn, answer['task'])['generation'])
        helpers.apply(conn, answer)
        assert kb.get_task(conn, answer['task']).status == 'ready'


def test_duplicate_owner_choice_values_refused(kanban_home):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=json_set(human_request,'$.choices',json(?)) WHERE id=?",
                (json.dumps([dict(label='One', value='plan-a'), dict(label='Two', value='plan-a')]), answer['task']))
        answer['generation'] = ca.snapshot(conn, answer['task'])['generation']
        with pytest.raises(ca.AnswerRefused, match='unknown_choice'):
            helpers.apply(conn, answer)
        assert not helpers.comments(conn, answer['task'])


@pytest.mark.parametrize('change', ['ask', 'containment'])
def test_other_fence_rechecks_after_preflight(kanban_home, monkeypatch, change):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        answer.update(mode='comment', choice='Need details')
        write_txn = kb.write_txn
        @contextmanager
        def raced(c, *args, **kwargs):
            monkeypatch.setattr(kb, 'write_txn', write_txn)
            with write_txn(c):
                if change == 'ask':
                    c.execute("UPDATE tasks SET human_request=json_set(human_request,'$.action','Changed') WHERE id=?", (answer['task'],))
                else:
                    c.execute('CREATE TABLE containment_observations(task_id TEXT, episode TEXT)')
                    c.execute("INSERT INTO containment_observations VALUES (?, 'held')", (answer['task'],))
            with write_txn(c, *args, **kwargs):
                yield
        monkeypatch.setattr(kb, 'write_txn', raced)
        with pytest.raises(ca.AnswerRefused, match='stale_ask|containment_hold'):
            helpers.apply(conn, answer)
        assert not helpers.comments(conn, answer['task'])


@pytest.mark.parametrize('mode', ['choice', 'comment'])
def test_receipt_event_kind_must_match_mode(kanban_home, mode):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn)
        if mode == 'comment':
            answer.update(mode='comment', choice='Need details')
        receipt = helpers.apply(conn, answer)
        kind = 'unblocked' if mode == 'choice' else 'card_answer_recorded'
        opposite = 'card_answer_recorded' if mode == 'choice' else 'unblocked'
        assert ca._valid_receipt(conn, answer['task'], answer, ca.digest(answer), kind, receipt)
        assert not ca._valid_receipt(conn, answer['task'], answer, ca.digest(answer), opposite, receipt)


@pytest.mark.parametrize('edit', ['choice', 'text-intent', 'text-decision', 'provenance', 'execution', 'permissions'])
def test_source_decision_binding_independently_fenced(tmp_path, edit):
    from tests.hermes_cli import test_kanban_card_answer_source as src
    path, answer, origin, op, remote = src.fixture(tmp_path)
    if edit == 'choice':
        remote['choiceValues']['a'] = 'plan-b'
    else:
        answer.update(mode='comment', choice='Recorded text')
        op.update(provenance=dict(type='kanban_other', version=1))
        op['decision'].update(choiceId='answer-text', revisionText='Recorded text')
        op['intent']['revisionText'] = 'Recorded text'
        if edit == 'text-intent': op['intent']['revisionText'] = 'Changed'
        if edit == 'text-decision': op['decision']['revisionText'] = 'Changed'
        if edit == 'provenance': op['provenance'] = {}
        if edit == 'execution': op['intent']['executionAuthorized'] = True
        if edit == 'permissions': op['intent']['permissionGrants'] = ['network']
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE operations SET body=?', (json.dumps(op),))
        conn.execute('UPDATE kanban_projections SET snapshot=?', (json.dumps(dict(remote=remote)),))
    with pytest.raises(ca.AnswerRefused, match='source_choice_mismatch|source_comment_mismatch'):
        src.read(path, answer, origin)


@pytest.mark.parametrize('field,value', [('gateway', 'foreign'), ('profile', 'foreign')])
def test_source_runtime_identity_fence(monkeypatch, tmp_path, field, value):
    import copy
    from tests.hermes_cli import test_kanban_card_answer_proof as ph
    _, rows = ph.records()
    # Keep both authenticated legs mutually consistent, but foreign to this runtime.
    for row in rows.values():
        row['args']['source'][field] = value
    class Records:
        gateway = 'origin'
        def read(self, rid): return copy.deepcopy(rows[rid])
    monkeypatch.setattr(runtime, 'Records', Records)
    monkeypatch.setattr(runtime, '_profile', lambda: ('work', tmp_path))
    monkeypatch.setattr(runtime, 'verify', lambda *a, **kw: {})
    monkeypatch.setattr(runtime, 'recorded', lambda *a, **kw: pytest.fail('foreign operation read'))
    with pytest.raises(ca.AnswerRefused, match='source_route_mismatch'):
        runtime.source('proof')


def test_custom_profile_is_not_a_runtime_route(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'custom')
    with pytest.raises(ca.AnswerRefused, match='local_profile_unknown'):
        runtime._profile()


@pytest.mark.parametrize('field,value', [('id', 'different'), ('to_gw', 'foreign'), ('coll', 'other'),
    ('action', 'other'), ('key', 'foreign/default/task')])
def test_replay_still_checks_original_route(kanban_home, monkeypatch, field, value):
    from tests.hermes_cli import test_kanban_card_answer_runtime as rt
    answer, rows, _ = rt.setup(monkeypatch, kanban_home)
    runtime.owner('original', 'proof')
    rows['original'][field] = value
    rows.pop('proof')
    with pytest.raises(ca.AnswerRefused, match='owner_route_mismatch'):
        runtime.owner('original', 'proof')
    with kbc.connect_closing() as conn:
        assert len(helpers.comments(conn, answer['task'])) == 1
