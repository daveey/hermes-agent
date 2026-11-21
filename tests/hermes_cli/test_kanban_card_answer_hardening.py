"""Hardening: trusted board root, exact receipt replay, controlled source refusals, module CLI."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_proof as proof
from hermes_cli import kanban_card_answer_runtime as runtime
from hermes_cli import kanban_card_answer_source as source
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_card_answer as helpers
from tests.hermes_cli import test_kanban_card_answer_runtime as rt
from tests.hermes_cli import test_kanban_card_answer_source as src

kanban_home = helpers.kanban_home
REPO = Path(__file__).resolve().parents[2]


# --- trusted board root / profile coordinate -------------------------------------------------

def test_board_root_ignores_kanban_home_and_db_pins(kanban_home, monkeypatch, tmp_path):
    decoy = tmp_path / 'decoy'
    (decoy / 'kanban' / 'boards' / 'side').mkdir(parents=True)
    (decoy / 'kanban.db').write_bytes(b'')
    (decoy / 'kanban' / 'boards' / 'side' / 'kanban.db').write_bytes(b'')
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(decoy))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(decoy / 'kanban.db'))
    assert runtime._board_db('default') == (kanban_home / 'kanban.db').resolve()
    with pytest.raises(ca.AnswerRefused, match='owner_board_missing'):
        runtime._board_db('side')  # exists only under the untrusted pin
    real = kanban_home / 'kanban' / 'boards' / 'side'
    real.mkdir(parents=True)
    (real / 'kanban.db').write_bytes(b'')
    assert runtime._board_db('side') == (real / 'kanban.db').resolve()


def test_owner_applies_in_trusted_home_despite_kanban_home_pin(kanban_home, monkeypatch, tmp_path):
    answer, _, _ = rt.setup(monkeypatch, kanban_home)
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'elsewhere'))
    assert runtime.owner('original', 'proof')['operationId'] == answer['operationId']
    with kbc.connect_closing(kanban_home / 'kanban.db') as conn:
        assert kb.get_task(conn, answer['task']).status == 'ready'


def test_symlinked_board_cannot_escape_boards_root(kanban_home, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'kanban.db').write_bytes(b'')
    (kanban_home / 'kanban' / 'boards').mkdir(parents=True)
    (kanban_home / 'kanban' / 'boards' / 'link').symlink_to(outside)
    with pytest.raises(ca.AnswerRefused, match='owner_board_invalid'):
        runtime._board_db('link')


@pytest.mark.parametrize('board', ['../x', 'A', '', None, 'a/b', 5])
def test_board_slug_validated(kanban_home, board):
    with pytest.raises(ca.AnswerRefused, match='owner_board_invalid'):
        runtime._board_db(board)


def test_profile_is_routing_coordinate_not_assignee(kanban_home, monkeypatch):
    answer, _, _ = rt.setup(monkeypatch, kanban_home)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET assignee=? WHERE id=?', ('some-other-profile', answer['task']))
    runtime.owner('original', 'proof')
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, answer['task'])
        assert task.status == 'ready' and task.assignee == 'some-other-profile'


# --- exact receipt replay --------------------------------------------------------------------

def _tamper_event(conn, tid, kind, edit):
    with kb.write_txn(conn):
        row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC",
                           (tid, kind)).fetchone()
        payload = json.loads(row[1])
        edit(payload)
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row[0]))


def _receipt_edit(fn):
    return lambda p: fn(p[ca.FIELD])


RECEIPT_EDITS = {
    'origin_removed': _receipt_edit(lambda r: r.pop('originGateway')),
    'origin_empty': _receipt_edit(lambda r: r.update(originGateway='')),
    'extra_field': _receipt_edit(lambda r: r.update(extra=1)),
    'destination': _receipt_edit(lambda r: r.update(destination='elsewhere')),
    'execution_started': _receipt_edit(lambda r: r.update(executionStarted=True)),
    'execution_missing_bool': _receipt_edit(lambda r: r.update(executionStarted=0)),
    'provenance': _receipt_edit(lambda r: r.update(decisionProvenance='transcript')),
    'choice': _receipt_edit(lambda r: r.update(choice='plan-b')),
    'generation': _receipt_edit(lambda r: r.update(generation='b' * 64)),
}


@pytest.mark.parametrize('name', sorted(RECEIPT_EDITS))
def test_replay_refuses_inexact_receipt(kanban_home, name):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        helpers.apply(conn, req)
        _tamper_event(conn, req['task'], 'unblocked', RECEIPT_EDITS[name])
        with pytest.raises(ca.AnswerRefused, match='operation_(receipt_invalid|conflict)'):
            helpers.apply(conn, req, lambda _: pytest.fail('replay must not reach the origin'))


def test_replay_fingerprint_forgery_cannot_make_receipt(kanban_home):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        helpers.apply(conn, req)
        _tamper_event(conn, req['task'], 'unblocked', _receipt_edit(lambda r: r.update(fingerprint='f' * 64)))
        with pytest.raises(ca.AnswerRefused, match='operation_conflict'):
            helpers.apply(conn, req)


COMMENT_EDITS = {
    'provenance_origin': lambda p: p['recorded_decision'].update(originGateway='forged'),
    'provenance_choice': lambda p: p['recorded_decision'].update(choice='plan-b'),
    'provenance_removed': lambda p: p.pop('recorded_decision'),
    'relayed_human': lambda p: p.update(relayed_human={'message_id': 'x'}),
    'writer_human': lambda p: p.update(writer_kind='human'),
    'author': lambda p: p.update(author='someone'),
    'comment_id': lambda p: p.update(comment_id=10**9),
}


@pytest.mark.parametrize('name', sorted(COMMENT_EDITS))
def test_replay_requires_recorded_decision_comment_provenance(kanban_home, name):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        helpers.apply(conn, req)
        _tamper_event(conn, req['task'], 'commented', COMMENT_EDITS[name])
        with pytest.raises(ca.AnswerRefused, match='operation_receipt_invalid'):
            helpers.apply(conn, req, lambda _: pytest.fail('replay must not reach the origin'))


@pytest.mark.parametrize('body', ['hello', 'Choice recorded in Desktop (remote): {"value":"plan-b"}\nOperation: desktop-operation-one',
                                  'Choice recorded in Desktop (remote): not-json\nOperation: desktop-operation-one'])
def test_replay_requires_exact_comment_body(kanban_home, body):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        helpers.apply(conn, req)
        with kb.write_txn(conn):
            conn.execute('UPDATE task_comments SET body=? WHERE task_id=?', (body, req['task']))
        with pytest.raises(ca.AnswerRefused, match='operation_receipt_invalid'):
            helpers.apply(conn, req)


def test_replay_refuses_missing_comment_and_duplicate_provenance(kanban_home):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        helpers.apply(conn, req)
        with kb.write_txn(conn):
            row = conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind='commented'",
                               (req['task'],)).fetchone()
            kb._append_event(conn, req['task'], 'commented', json.loads(row[1]))
        with pytest.raises(ca.AnswerRefused, match='operation_receipt_invalid'):
            helpers.apply(conn, req)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute('DELETE FROM task_comments')
        with pytest.raises(ca.AnswerRefused, match='operation_receipt_invalid'):
            helpers.apply(conn, req)


def test_archived_receipt_must_be_archive_choice(kanban_home):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        first = helpers.apply(conn, req)
        assert helpers.apply(conn, req, lambda _: pytest.fail('offline replay')) == first
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET kind='archived' WHERE task_id=? AND kind='unblocked'", (req['task'],))
        with pytest.raises(ca.AnswerRefused, match='operation_receipt_invalid'):
            helpers.apply(conn, req)


def test_malformed_event_payload_is_not_a_receipt_or_crash(kanban_home):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                         (req['task'], 'unblocked', '{not json', 1))
            conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                         (req['task'], 'commented', '[1]', 1))
        req['generation'] = ca.snapshot(conn, req['task'])['generation']
        assert helpers.apply(conn, req)['operationId'] == req['operationId']


# --- malformed source store ------------------------------------------------------------------

def _set_body(path, edit):
    with sqlite3.connect(path) as conn:
        body = json.loads(conn.execute('SELECT body FROM operations').fetchone()[0])
        edit(body)
        conn.execute('UPDATE operations SET body=?', (json.dumps(body),))


SOURCE_BODY_EDITS = {
    'no_intent': lambda b: b.pop('intent'),
    'no_decision': lambda b: b.pop('decision'),
    'intent_not_dict': lambda b: b.update(intent=5),
    'no_request_id': lambda b: b.pop('requestId'),
    'decision_not_dict': lambda b: b.update(decision='x'),
}


@pytest.mark.parametrize('name', sorted(SOURCE_BODY_EDITS))
def test_malformed_operation_body_is_controlled_refusal(tmp_path, name):
    path, answer, origin, _, _ = src.fixture(tmp_path)
    _set_body(path, SOURCE_BODY_EDITS[name])
    with pytest.raises(ca.AnswerRefused):
        src.read(path, answer, origin)


@pytest.mark.parametrize('table,column,value', [('operations', 'body', '{bad'), ('operations', 'body', '[]'),
                                                ('kanban_projections', 'snapshot', '{bad'),
                                                ('kanban_projections', 'snapshot', '{}'),
                                                ('kanban_projections', 'request', '{"x":1}'),
                                                ('kanban_projections', 'request', 'null')])
def test_malformed_stored_json_is_controlled_refusal(tmp_path, table, column, value):
    path, answer, origin, _, _ = src.fixture(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE {table} SET {column}=?', (value,))
    with pytest.raises(ca.AnswerRefused):
        src.read(path, answer, origin)


def test_missing_or_schemaless_store_refused_without_creation(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    with pytest.raises(ca.AnswerRefused, match='source_store_unavailable'):
        src.read(path, {'operationId': 'x'}, dict(gateway='origin', profile='work', session='chat-one'))
    assert not path.exists()
    empty = tmp_path / 'empty.sqlite3'
    sqlite3.connect(empty).close()
    with pytest.raises(ca.AnswerRefused, match='source_store_unavailable'):
        src.read(empty, {'operationId': 'x'}, dict(gateway='origin', profile='work', session='chat-one'))
    garbage = tmp_path / 'garbage.sqlite3'
    garbage.write_bytes(b'not a database' * 100)
    with pytest.raises(ca.AnswerRefused, match='source_operation_malformed'):
        src.read(garbage, {'operationId': 'x'}, dict(gateway='origin', profile='work', session='chat-one'))


def test_busy_store_is_retryable_not_refusal(tmp_path):
    path, answer, origin, _, _ = src.fixture(tmp_path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute('BEGIN EXCLUSIVE')
    try:
        reader_error = None
        try:
            src.read(path, answer, origin)
        except OSError as exc:
            reader_error = exc
        except ca.AnswerRefused:
            pytest.fail('lock contention must not be a terminal refusal')
        # Journal-mode dependent: a rollback-journal read may or may not block; when it
        # does, it must surface as retryable.
        assert reader_error is None or str(reader_error) == 'source_store_busy'
    finally:
        holder.execute('ROLLBACK')
        holder.close()


@pytest.mark.parametrize('bad', [None, [], 'x', {}, {'ownerGateway': 'owner'}, {'a': 1}])
def test_proof_refuses_malformed_answer_without_keyerror(bad):
    with pytest.raises(ca.AnswerRefused, match='source_request_invalid'):
        proof.verify(bad, request_id='original', proof_id='proof', owner_gateway='owner',
                     read_request=lambda rid: {})


# --- module CLI -------------------------------------------------------------------------------

def run_main(capsys, argv):
    code = runtime.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_cli_owner_and_source_json_contract(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(runtime, 'owner', lambda r, p: seen.append(('owner', r, p)) or {'receipt': 1})
    monkeypatch.setattr(runtime, 'source', lambda p: seen.append(('source', p)) or {'proof': 2})
    assert run_main(capsys, ['owner', 'req', 'prf']) == (0, {'ok': True, 'result': {'receipt': 1}})
    assert run_main(capsys, ['source', 'prf']) == (0, {'ok': True, 'result': {'proof': 2}})
    assert seen == [('owner', 'req', 'prf'), ('source', 'prf')]


@pytest.mark.parametrize('argv', [[], ['owner'], ['owner', 'a'], ['owner', 'a', 'b', 'c'], ['source'],
                                  ['source', 'a', 'b'], ['apply', 'a'], ['--recorded', 'owner', 'a', 'b']])
def test_cli_usage_never_calls_functions(monkeypatch, capsys, argv):
    monkeypatch.setattr(runtime, 'owner', lambda *a: pytest.fail('called'))
    monkeypatch.setattr(runtime, 'source', lambda *a: pytest.fail('called'))
    assert run_main(capsys, argv) == (64, {'ok': False, 'error': 'usage'})


@pytest.mark.parametrize('exc,code,body', [
    (ca.AnswerRefused('stale_ask'), 2, {'ok': False, 'refused': 'stale_ask'}),
    (OSError('/secret/path'), 3, {'ok': False, 'retryable': True, 'error': 'unavailable'}),
    (sqlite3.OperationalError('locked'), 3, {'ok': False, 'retryable': True, 'error': 'unavailable'}),
    (RuntimeError('token=abc'), 1, {'ok': False, 'error': 'internal_error', 'type': 'RuntimeError'}),
])
def test_cli_error_envelopes_leak_nothing(monkeypatch, capsys, exc, code, body):
    def boom(*_):
        raise exc
    monkeypatch.setattr(runtime, 'owner', boom)
    monkeypatch.setattr(runtime, 'source', boom)
    for argv in (['owner', 'a', 'b'], ['source', 'a']):
        got_code, got = run_main(capsys, argv)
        assert (got_code, got) == (code, body)


def test_cli_real_owner_refusal_leaves_card_untouched(kanban_home, monkeypatch, capsys):
    answer, rows, _ = rt.setup(monkeypatch, kanban_home)
    rows['proof']['state'] = 'pending'
    code, out = run_main(capsys, ['owner', 'original', 'proof'])
    assert code == 2 and out['refused'] == 'source_proof_unavailable'
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, answer['task']).status == 'blocked'
    rows['proof']['state'] = 'done'
    code, out = run_main(capsys, ['owner', 'original', 'proof'])
    assert code == 0 and out['result']['operationId'] == answer['operationId']


def test_cli_real_source_malformed_store_is_refusal(monkeypatch, capsys, tmp_path):
    _answer, rows = rt.proof_helpers.records()
    rows['proof'].pop('result')
    rows['proof']['state'] = 'pending'

    class Records:
        gateway = 'origin'
        def read(self, rid):
            return copy.deepcopy(rows[rid])
    monkeypatch.setattr(runtime, 'Records', Records)
    monkeypatch.setattr(runtime, '_profile', lambda: ('work', tmp_path))
    code, out = run_main(capsys, ['source', 'proof'])
    assert (code, out) == (2, {'ok': False, 'refused': 'source_store_unavailable'})


def test_module_entry_point_subprocess(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_')}
    env.update(HERMES_HOME=str(tmp_path / 'home'), HOME=str(tmp_path), PYTHONPATH=str(REPO))
    done = subprocess.run([sys.executable, '-m', 'hermes_cli.kanban_card_answer_runtime', 'owner', 'only-one'],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 64
    assert json.loads(done.stdout) == {'ok': False, 'error': 'usage'}
