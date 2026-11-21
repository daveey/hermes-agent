"""Session context is a fresh owner observation, not a replayable write receipt."""
import asyncio
import copy
from dataclasses import replace

import pytest

from agent.runtime_session_store import WorkerPersistenceError
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch
from tests.gateway import test_worker_turn_lease_query as fixtures

worker = fixtures.worker


def test_context_read_is_fresh_without_journal_receipt_or_revision_writes(worker):
    store, db = worker.store, worker.db
    before = copy.deepcopy(store.journal)
    disk = store.path.read_bytes()
    sequence = db._read_one('SELECT last_sequence FROM worker_executions')[0]
    receipts = db._read_one('SELECT COUNT(*) FROM worker_receipts')[0]
    revision = db.get_session('owned')['runtime_revision']
    assert store.get_session('owned')['title'] == db.get_session('owned')['title']
    db._write_sql("UPDATE sessions SET title='fresh' WHERE id='owned'")
    assert store.get_session('owned')['title'] == 'fresh'
    assert store.journal == before and store.path.read_bytes() == disk
    assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == sequence
    assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == receipts
    assert db.get_session('owned')['runtime_revision'] == revision


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'session_id': 'foreign'}, 'permission_denied'),
    ({'profile_id': 'foreign'}, 'profile_mismatch'),
    ({'generation': 1}, 'stale_generation'),
    ({'generation': True}, 'stale_generation'),
    ({'epoch': 0}, 'stale_epoch'),
    ({'epoch': True}, 'stale_epoch'),
    ({'secret': 'x' * 32}, 'permission_denied'),
    ({'birth': -1}, 'permission_denied'),
    ({'execution_id': 'missing'}, 'not_found'),
    ({'target': ''}, 'invalid_params'),
    ({'target': None}, 'invalid_params'),
    ({'sequence': 1}, 'invalid_params'),
])
def test_context_rejects_invalid_scope(worker, changes, reason):
    with pytest.raises(RuntimeStoreError, match=f'^{reason}$'):
        worker.rpc('worker.sessionContext', **(dict(worker.store.scope, target='owned') | changes))


@pytest.mark.parametrize(('sql', 'reason'), [
    ('UPDATE sessions SET runtime_generation=runtime_generation+1', 'stale_generation'),
    ('UPDATE worker_executions SET owner_epoch=0', 'stale_epoch'),
    ("UPDATE worker_executions SET status='unknown'", 'stale_generation'),
    ("UPDATE worker_executions SET status='terminal'", 'stale_generation'),
])
def test_context_refuses_invalidated_execution(worker, sql, reason):
    assert worker.store.get_session('owned')
    worker.db._write_sql(sql)
    with pytest.raises(RuntimeStoreError, match=f'^{reason}$'):
        worker.store.get_session('owned')


def test_context_refuses_new_epoch_and_old_serving_connection(worker):
    worker.authority.epoch -= 1
    with pytest.raises(RuntimeStoreError, match='^stale_epoch$'):
        worker.store.get_session('owned')
    worker.authority.epoch += 1
    begin_runtime_epoch(worker.db, instance_id='replacement')
    with pytest.raises(RuntimeStoreError, match='^stale_epoch$'):
        worker.store.get_session('owned')


def test_context_requires_producer_capability_principal_and_liveness(worker):
    actor = worker.connection.actor
    worker.connection.actor = replace(actor, capabilities=frozenset({'session:control'}))
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        worker.store.get_session('owned')
    worker.connection.actor = replace(actor, subject='other')
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        worker.store.get_session('owned')
    worker.connection.actor = actor
    worker.child.terminate()
    worker.child.wait(timeout=5)
    with pytest.raises(RuntimeStoreError, match='^worker_not_live$'):
        worker.store.get_session('owned')


def test_context_lineage_authorization_and_rotation(worker):
    store = worker.store
    worker.db.create_session('foreign', 'cli')
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        store.get_session('foreign')
    old = dict(store.scope)
    assert store.try_acquire_compression_lock('owned', 'compressor')
    store.publish_compression_child(parent_session_id='owned', child_session_id='child', source='cli',
        messages=[{'role': 'assistant', 'content': 'summary'}], compression_lock_holder='compressor')
    assert store.get_session('child')['id'] == 'child'
    assert store.get_session('owned')['id'] == 'owned'
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        worker.rpc('worker.sessionContext', **old, target='owned')


@pytest.mark.parametrize('failed', [True, False])
def test_context_cannot_overtake_failed_or_pending_writes(worker, failed):
    store = worker.store
    if failed:
        store.failure = 'previous_write_failed'
    else:
        store.journal['pending'] = [{'sequence': 1, 'operation': 'test'}]
    calls = []
    store.rpc = lambda *args, **kwargs: calls.append(args)
    with pytest.raises(WorkerPersistenceError, match='previous_write_failed' if failed else 'pending_receipt'):
        store.get_session('owned')
    assert calls == []


def test_context_legacy_owner_fallback_only_for_method_not_found(worker):
    store, original = worker.store, worker.store.rpc
    calls = []

    def legacy(method, **params):
        calls.append(method)
        if method == 'worker.sessionContext':
            error = WorkerPersistenceError('Method not found')
            error.code = -32601
            raise error
        return original(method, **params)

    store.rpc = legacy
    assert store.get_session('owned')
    assert store.get_session('owned')
    assert calls.count('worker.sessionContext') == 1
    assert calls.count('worker.persist') == 2


@pytest.mark.parametrize('cached_legacy', [False, True])
def test_context_legacy_refused_target_does_not_poison_store(worker, cached_legacy):
    store, original = worker.store, worker.store.rpc
    worker.db.create_session('foreign', 'cli')
    calls = []

    def legacy(method, **params):
        calls.append((method, params.get('operation'), params.get('payload')))
        if method == 'worker.sessionContext':
            error = WorkerPersistenceError('Method not found')
            error.code = -32601
            raise error
        return original(method, **params)

    store.rpc = legacy
    if cached_legacy:
        assert store.get_session('owned')['id'] == 'owned'
        calls.clear()
    with pytest.raises((WorkerPersistenceError, RuntimeStoreError), match='^permission_denied$'):
        store.get_session('foreign')
    assert store.failure is None
    assert store.journal['pending'] == []
    assert not any(operation == 'compression.context' for _, operation, _ in calls)
    assert store.get_session('owned')['id'] == 'owned'
    assert store.try_acquire_compression_lock('owned', 'after-refused-read')
    store.publish_compression_child(parent_session_id='owned', child_session_id='child', source='cli',
        messages=[{'role': 'assistant', 'content': 'summary'}], compression_lock_holder='after-refused-read')
    assert store.get_session('child')['id'] == 'child'
    assert store.get_session('owned')['id'] == 'owned'


@pytest.mark.parametrize('code', [None, 4001, -32603])
def test_context_does_not_downgrade_other_owner_errors(worker, code):
    calls = []
    error = WorkerPersistenceError('Method not found')
    error.code = code

    def refused(method, **params):
        calls.append(method)
        raise error

    worker.store.rpc = refused
    with pytest.raises(WorkerPersistenceError) as caught:
        worker.store.get_session('owned')
    assert caught.value is error
    assert calls == ['worker.sessionContext']


def test_context_transport_retry_is_a_fresh_read_not_a_write(worker):
    store, original = worker.store, worker.store.rpc
    before = copy.deepcopy(store.journal)
    calls = []

    def once(method, **params):
        calls.append(method)
        if len(calls) == 1:
            worker.db._write_sql("UPDATE sessions SET title='after reconnect' WHERE id='owned'")
            raise ConnectionError('owner link closed')
        return original(method, **params)

    store.rpc = once
    assert store.get_session('owned')['title'] == 'after reconnect'
    assert calls == ['worker.sessionContext', 'worker.sessionContext']
    assert store.journal == before


def test_context_single_read_transaction(worker, monkeypatch):
    import hermes_state_runtime as runtime
    original = runtime._worker_assignment
    observed = []

    def race(conn, *args):
        row = original(conn, *args)
        observed.append(conn.in_transaction)
        worker.db._write_sql("UPDATE sessions SET title='later' WHERE id='owned'")
        return row

    first = worker.db.get_session('owned')['title']
    monkeypatch.setattr(runtime, '_worker_assignment', race)
    assert worker.store.get_session('owned')['title'] == first
    assert observed == [True]
    monkeypatch.setattr(runtime, '_worker_assignment', original)
    assert worker.store.get_session('owned')['title'] == 'later'


def test_context_authenticated_wire_contract(worker):
    params = dict(worker.store.scope, target='owned')
    request = {'jsonrpc': '2.0', 'id': 'context', 'method': 'worker.sessionContext', 'params': params}
    reply = asyncio.run(worker.connection.dispatch(request))
    assert reply['result']['session']['id'] == 'owned'
    refused = asyncio.run(worker.connection.dispatch(dict(request, params=dict(params, sequence=1))))
    assert refused['error']['data']['reason'] == 'invalid_params'
