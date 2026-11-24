"""Local source SQL and actual receiver/intake integration, using synthetic source rows."""
import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_selfwork_exception as sx
from hermes_cli import kanban_selfwork_local as local
from hermes_cli import kanban_selfwork_local_source as source
from tests.hermes_cli import test_kanban_selfwork_exception as helpers

world = helpers.world
nochat_envelope = helpers.nochat_envelope
request_envelope = helpers.request_envelope
home = helpers.home


@pytest.fixture
def operation(home, monkeypatch):
    from hermes_cli import install_identity
    monkeypatch.setattr(install_identity, 'get_install_id', lambda: 'test-install')
    helpers.offer(home)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, home['task'])
        ask = json.loads(task.human_request) if isinstance(task.human_request, str) else task.human_request
        generation = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (home['task'],)).fetchone()[0]
    owner = dict(gatewayId='install:test-install', profile='default', storedSessionId='creator')
    request = dict(requestId='local-request', revision=1, owner=owner, choices=[
        dict(id='card-0', effect='answer', label='Unblock'), dict(id='card-1', effect='answer', label='Archive')])
    decision = dict(requestId=request['requestId'], expectedRevision=1, choiceId='card-0',
                    revisionText='Unblock', idempotencyKey='local-key')
    op = dict(operationId=local.RECOVERY_OPERATION, owner=owner, requestId=request['requestId'], revision=1,
              idempotencyKey='local-key', recordedAt='2026-01-01T00:00:00+00:00', dispatchState='delivered',
              intent=dict(request=request, effect='answer', revisionText='Unblock', executionAuthorized=False,
                          permissionGrants=[]), decision=decision)
    receipt = dict(destination='kanban_ask', board='default', task=home['task'], operationId=op['operationId'],
                   value='unblock', executionStarted=False, fingerprint=source.digest([op['intent'], decision]))
    op['receipt'] = receipt
    snapshot = dict(task=home['task'], session='creator', generation=generation, ask=ask, title=task.title)
    namespace = json.dumps(['install:test-install', 'default'], separators=(',', ':'))
    path = home['dir'] / 'user-steps/actions.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE requests(namespace TEXT,session TEXT,id TEXT,revision INTEGER,state TEXT,resolution TEXT)')
        conn.execute('CREATE TABLE revisions(namespace TEXT,id TEXT,revision INTEGER,body TEXT)')
        resolution = dict(choiceId='card-0', operationId=op['operationId'], revision=1, recordedAt=op['recordedAt'])
        conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
                     (namespace, 'creator', request['requestId'], 1, 'open', json.dumps(resolution)))
        conn.execute('INSERT INTO revisions VALUES(?,?,?,?)', (namespace, request['requestId'], 1, json.dumps(request)))
        conn.execute('CREATE TABLE operations(namespace TEXT,session TEXT,id TEXT,body TEXT)')
        conn.execute('CREATE TABLE kanban_projections(namespace TEXT,session TEXT,request_id TEXT,board TEXT,task TEXT,request TEXT,snapshot TEXT)')
        conn.execute('INSERT INTO operations VALUES(?,?,?,?)', (namespace, 'creator', op['operationId'], json.dumps(op)))
        conn.execute('INSERT INTO kanban_projections VALUES(?,?,?,?,?,?,?)',
                     (namespace, 'creator', request['requestId'], 'default', home['task'], json.dumps(request), json.dumps(snapshot)))
    # Seed the historical old-plugin/old-core plain unblock; new core refuses it.
    with monkeypatch.context() as old_core, kbc.connect_closing() as conn:
        old_core.setattr(sx, 'missing_local_adapter', lambda *a: False)
        kb.unblock_task(conn, home['task'], comment=('user-steps', 'Choice recorded in Desktop: '
                        + source.encoded(dict(label='Unblock', value='unblock')) + '\nOperation: ' + op['operationId']),
                        event_extra={'user_steps_projection': receipt})
    return dict(path=path, op=op, snapshot=snapshot)


def recover(home):
    with kbc.connect_closing() as conn:
        return local.recover(conn, home['hub'], local.RECOVERY_OPERATION)


def test_recover_then_admit_once_preserves_history(home, operation):
    original_bytes = operation['path'].read_bytes()
    with kbc.connect_closing() as conn:
        task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (home['task'],)).fetchone())
        events = [tuple(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (home['task'],))]
    grant = recover(home)
    assert recover(home) == grant
    assert operation['path'].read_bytes() == original_bytes
    with kbc.connect_closing() as conn:
        assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (home['task'],)).fetchone()) == task
        after = [tuple(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (home['task'],))]
        assert after[:len(events)] == events and len(after) == len(events) + 1
    result = helpers.reconsider(home)
    assert result['created'] is True, result
    assert helpers.reconsider(home)['replayed'] is True
    assert recover(home) == grant  # receipt replay is not renewed authority
    assert len(helpers.state(home)['status']['spent']) == 1
    assert helpers.state(home)['status']['local_authority']['usable'] is False


def test_recovery_rollback_and_concurrent_exact_replay(home, operation, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    append = kb._append_event

    def fail(conn, task, kind, *args, **kwargs):
        value = append(conn, task, kind, *args, **kwargs)
        if kind == local.RECOVERED:
            raise RuntimeError('crash before commit')
        return value
    with monkeypatch.context() as patch:
        patch.setattr(kb, '_append_event', fail)
        with pytest.raises(RuntimeError, match='crash before commit'):
            recover(home)
    with kbc.connect_closing() as conn:
        assert sx._own(conn, local.RECOVERED) == []
    barrier = Barrier(2)

    def concurrent():
        barrier.wait(timeout=10)
        return recover(home)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(concurrent) for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]
    assert results[0] == results[1]
    with kbc.connect_closing() as conn:
        assert len(sx._own(conn, local.RECOVERED)) == 1


@pytest.mark.parametrize('mutation', ['board', 'task', 'session', 'remote', 'custom', 'ask', 'generation',
                                     'revision', 'resolution', 'withdrawn', 'frozen', 'foreign_event', 'comment', 'expiry'])
def test_changed_projection_or_owner_evidence_refuses(home, operation, monkeypatch, mutation):
    if mutation in ('board', 'task', 'session', 'remote', 'custom', 'ask', 'generation'):
        with sqlite3.connect(operation['path']) as conn:
            snapshot = copy.deepcopy(operation['snapshot'])
            if mutation in ('board', 'task'):
                conn.execute('UPDATE kanban_projections SET ' + mutation + '=?', ('wrong',))
            else:
                targets = {
                    'session': (snapshot, 'session', 'other'),
                    'remote': (snapshot, 'remote', {}),
                    'custom': (snapshot['ask'], 'choices', [{'label': 'Unblock', 'value': 'unblock'}]),
                    'ask': (snapshot['ask'], 'action', snapshot['ask']['action'] + ' changed'),
                    'generation': (snapshot, 'generation', 'not-an-integer'),
                }
                target, key, value = targets[mutation]
                target[key] = value
                conn.execute('UPDATE kanban_projections SET snapshot=?', (json.dumps(snapshot),))
    elif mutation in ('revision', 'resolution', 'withdrawn', 'frozen'):
        with sqlite3.connect(operation['path']) as conn:
            conn.execute({
                'revision': 'UPDATE requests SET revision=2',
                'resolution': "UPDATE requests SET resolution='{}'",
                'withdrawn': "UPDATE requests SET state='withdrawn'",
                'frozen': "UPDATE revisions SET body='{}'",
            }[mutation])
    elif mutation == 'expiry':
        monkeypatch.setattr(local.time, 'time', lambda: operation['snapshot']['ask'][sx.STAMP]['expires_at'])
    else:
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            if mutation == 'comment':
                conn.execute("UPDATE task_comments SET body='changed' WHERE task_id=? AND author='user-steps'", (home['task'],))
            else:
                cols = {r[1] for r in conn.execute('PRAGMA table_info(task_events)')}
                if 'origin_gateway' not in cols:
                    conn.execute('ALTER TABLE task_events ADD COLUMN origin_gateway TEXT')
                conn.execute("UPDATE task_events SET origin_gateway='foreign' WHERE task_id=? AND kind='unblocked'", (home['task'],))
    with pytest.raises(sx.Refused):
        recover(home)
    with kbc.connect_closing() as conn:
        assert sx._own(conn, local.RECOVERED) == []


@pytest.mark.parametrize('mutation', ['owner', 'revision', 'intent', 'decision', 'cancelled', 'pending', 'receipt'])
def test_source_mutation_refuses_without_writes(home, operation, mutation):
    op = copy.deepcopy(operation['op'])
    targets = {
        'owner': (op['owner'], 'profile', 'other'),
        'revision': (op, 'revision', 2),
        'intent': (op['intent'], 'permissionGrants', ['forged']),
        'decision': (op['decision'], 'choiceId', 'card-1'),
        'cancelled': (op, 'dispatchState', 'cancelled'),
        'pending': (op, 'dispatchState', 'pending'),
        'receipt': (op['receipt'], 'value', 'archive'),
    }
    target, key, value = targets[mutation]
    target[key] = value
    with sqlite3.connect(operation['path']) as conn:
        conn.execute('UPDATE operations SET body=?', (json.dumps(op),))
    with pytest.raises(sx.Refused):
        recover(home)
    with kbc.connect_closing() as conn:
        assert sx._own(conn, local.RECOVERED) == []


@pytest.mark.parametrize('mutation', ['replacement', 'decline', 'hold', 'source'])
def test_revalidate_after_issuance_before_consumption(home, operation, monkeypatch, mutation):
    recover(home)
    if mutation == 'source':
        with sqlite3.connect(operation['path']) as conn:
            op = copy.deepcopy(operation['op'])
            op['intent']['revisionText'] = 'changed'
            conn.execute('UPDATE operations SET body=?', (json.dumps(op),))
    elif mutation == 'hold':
        from hermes_cli import kanban_containment_guard
        monkeypatch.setattr(kanban_containment_guard, 'pending', lambda *a: True)
    else:
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            if mutation == 'decline':
                kb._append_event(conn, home['task'], 'archived', {})
            else:
                prior = sx._own(conn, sx.OFFERED)[0]['payload']
                newer = copy.deepcopy(prior)
                newer[sx.STAMP]['offer_uid'] = 'replacement'
                kb._append_event(conn, home['task'], sx.OFFERED, newer)
    try:
        result = helpers.reconsider(home)
    except sx.Refused:
        result = {'created': False}
    assert result.get('created') is not True
    with kbc.connect_closing() as conn:
        assert sx._own(conn, sx.SPENT) == []
