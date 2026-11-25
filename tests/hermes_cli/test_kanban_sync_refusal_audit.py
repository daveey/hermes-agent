"""Server-receipted policy refusals stay visible without masquerading as drift."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_refusal_audit as audit
from hermes_cli.kanban_history_residue_source import Refused


@pytest.fixture
def case(tmp_path, monkeypatch):
    conns = [sqlite3.connect(tmp_path / name, isolation_level=None) for name in ('live.db', 'shadow.db')]
    for conn in conns:
        conn.row_factory = sqlite3.Row
        conn.executescript('''
          CREATE TABLE tasks(id TEXT, current_run_id INTEGER, sync_version INTEGER, created_on TEXT, title TEXT);
          INSERT INTO tasks VALUES ('t_card',NULL,1,'home','same');
          CREATE TABLE task_runs(id INTEGER, uid TEXT, task_id TEXT, ended_at INTEGER);
          CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
          CREATE TABLE task_comments(task_id TEXT, uid TEXT);
          CREATE TABLE task_chat_links(task_id TEXT, uid TEXT, chat_gateway TEXT);
          CREATE TABLE kanban_sync_state(board_id TEXT, gateway_id TEXT, cursor INTEGER);
          INSERT INTO kanban_sync_state VALUES ('default','home',100);
          CREATE TABLE kanban_sync_outbox(op_id TEXT, table_name TEXT, row_uid TEXT, op TEXT, fields TEXT,
                                         state TEXT, last_error TEXT);
        ''')
    conns[0].execute("INSERT INTO kanban_sync_outbox VALUES ('rejected-op','task_events','event','upsert','{}','rejected','spend_decision_pending: stopped')")
    proof = dict(version=1, board='default', gateway='home', execution_authority=False,
                 historical_identity_certified=False, receipt=dict(client_op_id='rejected-op', status='rejected',
                    reason='spend_decision_pending', detail='stopped', payload_sha256=None))
    calls = []
    def receipt(hub, board, op_id):
        assert all(not c.in_transaction for c in conns)
        calls.append(op_id)
        return copy.deepcopy(proof)
    monkeypatch.setattr(audit, '_receipt', receipt)
    hub = SimpleNamespace(ident=SimpleNamespace(url='https://example.test', gateway='home', key='key'),
                          changes=lambda *a: dict(changes=[],more=False,head=100,next=100))
    yield conns, hub, proof, calls
    for conn in conns:
        conn.close()


def test_retained_matching_refusal_preserves_rows_and_independent_drift(case):
    (live, shadow), hub, _proof, calls = case
    before = [list(c.iterdump()) for c in (live, shadow)]
    report = audit.report(live, shadow, hub, 'default')
    assert report['clean'] and report['rejected_ops'] == 1
    assert report['audited_server_refusals'] == ['rejected-op']
    assert calls == ['rejected-op']
    assert [list(c.iterdump()) for c in (live, shadow)] == before
    assert not report['server_refusal_evidence']['historical_identity_certified']
    shadow.execute("UPDATE tasks SET title='actual drift'")
    assert not audit.report(live, shadow, hub, 'default')['clean']
    shadow.execute("UPDATE tasks SET title='same'")
    live.execute("INSERT INTO kanban_sync_outbox VALUES ('new-op','task_events','event2','upsert','{}','rejected','conflict')")
    assert not audit.report(live, shadow, hub, 'default')['clean']


def test_existing_retain_audit_composes_without_double_counting(case):
    import hashlib
    (live, shadow), hub, _proof, _calls = case
    for conn in (live, shadow):
        conn.execute("INSERT INTO task_runs VALUES (1,'run','t_card',123)")
    live.execute('''CREATE TABLE kanban_failure_commands(
        run_uid TEXT, task_id TEXT, op_id TEXT, operation_kind TEXT,
        request_uid TEXT, event_uid TEXT, request_json TEXT, request_sha256 TEXT,
        delivery_state TEXT, canonical_receipt_json TEXT)''')
    wire = dict(op='retain', client_op_id='retain-op', task='t_card', run_uid='run', event_uid='event')
    text = json.dumps(wire, sort_keys=True, separators=(',', ':'))
    live.execute('INSERT INTO kanban_failure_commands VALUES (?,?,?,?,?,?,?,?,?,?)',
                 ('run','t_card','retain-op','retain','retain-op','event',text,
                  hashlib.sha256(text.encode()).hexdigest(),'rejected',None))
    live.execute("INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)",
                 ('retain-op','tasks','t_card','retain',text,'rejected','missing_decision_episode'))
    before = [list(c.iterdump()) for c in (live, shadow)]
    result = audit.report(live, shadow, hub, 'default')
    assert result['audited_retain_rejections'] == ['retain-op']
    assert result['audited_server_refusals'] == ['rejected-op']
    assert result['rejected_ops'] == 2 and result['clean']
    assert [list(c.iterdump()) for c in (live, shadow)] == before
    shadow.execute("UPDATE tasks SET title='still real divergence'")
    assert not audit.report(live, shadow, hub, 'default')['clean']


@pytest.mark.parametrize('fault', ['missing','applied','reason','detail','id','gateway','board','authority','identity','version'])
def test_unknown_or_conflicting_receipt_is_not_audited(case, fault):
    (live, shadow), hub, proof, _calls = case
    changes = {
        'missing': (proof, 'receipt', None), 'applied': (proof['receipt'], 'status', 'applied'),
        'reason': (proof['receipt'], 'reason', 'storage_error'), 'detail': (proof['receipt'], 'detail', 'different'),
        'id': (proof['receipt'], 'client_op_id', 'another-op'), 'gateway': (proof, 'gateway', 'other'),
        'board': (proof, 'board', 'other'), 'authority': (proof, 'execution_authority', True),
        'identity': (proof, 'historical_identity_certified', True), 'version': (proof, 'version', True),
    }
    target, key, value = changes[fault]
    target[key] = value
    result = audit.report(live, shadow, hub, 'default')
    assert not result['clean'] and not result['audited_server_refusals']


@pytest.mark.parametrize('fault', ['feed','outbox','other_writer','cursor','expired'])
def test_fresh_cut_races_fail_closed(case, monkeypatch, fault):
    (live, shadow), hub, _proof, _calls = case
    original = audit._receipt
    def external_write():
        path = live.execute('PRAGMA database_list').fetchone()[2]
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE tasks SET title='new local edit'")
    actions = {
        'feed': lambda: setattr(hub, 'changes', lambda *a: dict(changes=[],more=False,head=101,next=101)),
        'outbox': lambda: live.execute("UPDATE kanban_sync_outbox SET fields='changed'"),
        'cursor': lambda: shadow.execute('UPDATE kanban_sync_state SET cursor=101'),
        'expired': lambda: monkeypatch.setattr(audit.cut.time, 'monotonic', lambda: 100000000000.0),
        'other_writer': external_write,
    }
    def receipt(*args):
        result = original(*args)
        actions[fault]()
        return result
    monkeypatch.setattr(audit, '_receipt', receipt)
    with pytest.raises(Refused):
        audit.report(live, shadow, hub, 'default')


def test_read_path_is_get_only_and_exact_encoded_identity(case, monkeypatch):
    (_live, _shadow), hub, proof, _calls = case
    calls = []
    def request(remote, method, path, body):
        calls.append((method,path,body))
        return json.dumps(proof).encode()
    monkeypatch.setattr(audit.transport, '_request', request)
    monkeypatch.undo()
    # Exercise the real reader independently of the report fixture's replacement.
    from hermes_cli.kanban_sync_refusal_audit import _receipt
    monkeypatch.setattr(audit.transport, '_request', request)
    assert _receipt(hub, 'space board', 'op:exact-id') == proof
    assert calls == [('GET','/v2/boards/space%20board/operation-receipt?op_id=op%3Aexact-id',None)]
