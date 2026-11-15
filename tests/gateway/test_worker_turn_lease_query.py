"""Fresh authenticated lease observations are not immutable persistence receipts."""
import asyncio
import copy
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from agent.runtime_session_store import RuntimeSessionStore
from gateway.config import Platform
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_contract import SessionRef
from gateway.session_controls import AuthorityConnection
from hermes_cli.process_identity import worker_birth
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.fixture
def worker(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli')
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id='p', instance_id='owner', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='owner'))
    authority.sessions['owned'] = LiveSession(SimpleNamespace(platform=Platform.TELEGRAM, user_id='human'), 'route')
    connection = AuthorityConnection(authority, SimpleNamespace(), {
        'user_id': 'human', 'profile_id': 'p', 'instance_id': 'owner', 'capabilities': ['worker:adopt']})
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    scope = dict(profile_id='p', session_id='owned', execution_id='worker', generation=0,
                 pid=child.pid, birth=worker_birth(psutil.Process(child.pid)), secret='w' * 32)

    def rpc(method, **params):
        return asyncio.run(connection.handlers()[method](SessionRef('p', params['session_id']), params))

    store = None
    try:
        rpc('worker.register', **scope, kind='compute')
        adopted = rpc('worker.adopt', **scope)
        store = RuntimeSessionStore(rpc, dict(scope, epoch=adopted['owner_epoch']), tmp_path / 'private')
        yield SimpleNamespace(db=db, store=store, rpc=rpc, connection=connection, authority=authority, child=child)
    finally:
        if store is not None:
            store.failure = None
            store.journal['pending'] = []
            store.close()
        child.stdin.close()
        child.wait(timeout=5)
        db.close()


def test_fresh_query_observes_refresh_release_and_expiry_without_receipts(worker):
    db, store = worker.db, worker.store
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    before = copy.deepcopy(store.journal)
    disk = store.path.read_bytes()
    sequence = db._read_one('SELECT last_sequence FROM worker_executions')[0]
    receipts = db._read_one('SELECT COUNT(*) FROM worker_receipts')[0]
    first = store.session_turn_lease_expires_at('owned', 'holder')
    assert first == db.session_turn_lease_expires_at('owned', 'holder')
    assert store.session_turn_lease_expires_at('owned', 'foreign-holder') is None
    db._write_sql('UPDATE session_turn_leases SET expires_at=expires_at+60')
    assert store.session_turn_lease_expires_at('owned', 'holder') == first + 60
    db._write_sql('UPDATE session_turn_leases SET expires_at=1')
    assert store.session_turn_lease_expires_at('owned', 'holder') == 1.0
    db.release_session_turn_lease('owned', 'holder')
    assert store.session_turn_lease_expires_at('owned', 'holder') is None
    assert worker.rpc('worker.turnLease', **store.scope, holder='holder') == {'expires_at': None}
    assert store.journal == before
    assert store.path.read_bytes() == disk
    assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == sequence
    assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == receipts


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'session_id': 'foreign'}, 'permission_denied'),
    ({'profile_id': 'foreign'}, 'profile_mismatch'),
    ({'generation': 1}, 'stale_generation'),
    ({'generation': True}, 'stale_generation'),
    ({'epoch': 0}, 'stale_epoch'),
    ({'epoch': True}, 'stale_epoch'),
    ({'secret': 'x' * 32}, 'permission_denied'),
    ({'execution_id': 'missing'}, 'not_found'),
    ({'holder': ''}, 'invalid_params'),
    ({'holder': None}, 'invalid_params'),
    ({'sequence': 1}, 'invalid_params'),
])
@pytest.mark.parametrize('method', ['worker.turnLease', 'worker.nativeOrigin'])
def test_query_rejects_invalid_scope(worker, changes, reason, method):
    from hermes_state_runtime import RuntimeStoreError
    with pytest.raises(RuntimeStoreError, match=f'^{reason}$'):
        worker.rpc(method, **(dict(worker.store.scope, holder='holder') | changes))


@pytest.mark.parametrize(('sql', 'reason'), [
    ('UPDATE sessions SET runtime_generation=runtime_generation+1', 'stale_generation'),
    ('UPDATE worker_executions SET owner_epoch=0', 'stale_epoch'),
    ("UPDATE worker_executions SET status='unknown'", 'stale_generation'),
    ("UPDATE worker_executions SET status='terminal'", 'stale_generation'),
])
@pytest.mark.parametrize('method', ['worker.turnLease', 'worker.nativeOrigin'])
def test_query_refuses_freshly_invalidated_execution(worker, sql, reason, method):
    from hermes_state_runtime import RuntimeStoreError
    assert worker.store.try_acquire_session_turn_lease('owned', 'holder')
    assert worker.store.session_turn_lease_expires_at('owned', 'holder') is not None
    worker.db._write_sql(sql)
    with pytest.raises(RuntimeStoreError, match=f'^{reason}$'):
        worker.rpc(method, **worker.store.scope, holder='holder')


@pytest.mark.parametrize('method', ['worker.turnLease', 'worker.nativeOrigin'])
def test_query_refuses_new_owner_epoch(worker, method):
    from hermes_state_runtime import RuntimeStoreError
    assert worker.store.try_acquire_session_turn_lease('owned', 'holder')
    begin_runtime_epoch(worker.db, instance_id='replacement')
    with pytest.raises(RuntimeStoreError, match='^stale_epoch$'):
        worker.rpc(method, **worker.store.scope, holder='holder')


@pytest.mark.parametrize('method', ['worker.turnLease', 'worker.nativeOrigin'])
def test_query_refuses_superseded_serving_connection(worker, method):
    from hermes_state_runtime import RuntimeStoreError
    assert worker.store.try_acquire_session_turn_lease('owned', 'holder')
    # Caller and DB agree, but this not-yet-retired connection belongs to the old owner.
    worker.authority.epoch -= 1
    with pytest.raises(RuntimeStoreError, match='^stale_epoch$'):
        worker.rpc(method, **worker.store.scope, holder='holder')


@pytest.mark.parametrize('method', ['worker.turnLease', 'worker.nativeOrigin'])
def test_query_requires_live_producer_and_capability(worker, method):
    from dataclasses import replace
    from hermes_state_runtime import RuntimeStoreError
    actor = worker.connection.actor
    worker.connection.actor = replace(actor, capabilities=frozenset({'session:control'}))
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        worker.rpc(method, **worker.store.scope, holder='holder')
    worker.connection.actor = replace(actor, subject='other-principal')
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        worker.rpc(method, **worker.store.scope, holder='holder')
    worker.connection.actor = actor
    worker.child.terminate()
    worker.child.wait(timeout=5)
    with pytest.raises(RuntimeStoreError, match='^worker_not_live$'):
        worker.rpc(method, **worker.store.scope, holder='holder')


def test_query_does_not_touch_pending_or_failure_and_propagates_unavailable(worker):
    store = worker.store
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    original = store.rpc

    def unavailable(method, **params):
        raise TimeoutError('owner unavailable')

    store.rpc = unavailable
    with pytest.raises(TimeoutError):
        store.refresh_session_turn_lease('owned', 'holder')
    before = copy.deepcopy(store.journal)
    failure, disk = store.failure, store.path.read_bytes()
    with pytest.raises(TimeoutError, match='owner unavailable'):
        store.session_turn_lease_expires_at('owned', 'holder')
    store.rpc = original
    assert store.session_turn_lease_expires_at('owned', 'holder') is not None
    assert store.journal == before and store.failure == failure
    assert store.path.read_bytes() == disk


def test_query_after_real_compression_uses_current_scope_and_root_lease(worker):
    from agent.runtime_session_store import WorkerPersistenceError
    from hermes_state_runtime import RuntimeStoreError
    store = worker.store
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    expiry = store.session_turn_lease_expires_at('owned', 'holder')
    old_scope = dict(store.scope)
    assert store.try_acquire_compression_lock('owned', 'compressor')
    store.publish_compression_child(parent_session_id='owned', child_session_id='child', source='cli',
        messages=[{'role': 'assistant', 'content': 'summary'}], compression_lock_holder='compressor')
    assert store.scope['session_id'] == 'child'
    assert store.session_turn_lease_expires_at('child', 'holder') == expiry
    # DurableTurnLease may retain the old target for cleanup, but a query proves CURRENT assignment.
    with pytest.raises(WorkerPersistenceError, match='^permission_denied$'):
        store.session_turn_lease_expires_at('owned', 'holder')
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        worker.rpc('worker.turnLease', **old_scope, holder='holder')
    store.release_session_turn_lease('owned', 'holder')
    assert store.session_turn_lease_expires_at('child', 'holder') is None


def test_query_is_single_read_transaction_even_with_concurrent_owner_change(worker, monkeypatch):
    import hermes_state_runtime as runtime
    store, db = worker.store, worker.db
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    expiry = store.session_turn_lease_expires_at('owned', 'holder')
    assignment = runtime._worker_assignment
    observed = []

    def invalidate_after_assignment(conn, *args):
        row = assignment(conn, *args)
        observed.append(conn.in_transaction)
        # A separate writer commits between the fence and lease read. The query must retain its cut.
        db._write_sql('UPDATE session_turn_leases SET expires_at=1')
        return row

    monkeypatch.setattr(runtime, '_worker_assignment', invalidate_after_assignment)
    assert store.session_turn_lease_expires_at('owned', 'holder') == expiry
    assert observed == [True]
    monkeypatch.setattr(runtime, '_worker_assignment', assignment)
    assert store.session_turn_lease_expires_at('owned', 'holder') == 1.0


def test_authenticated_dispatch_and_closed_query_contract(worker):
    store = worker.store
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    params = dict(store.scope, holder='holder')
    request = {'jsonrpc': '2.0', 'id': 'lease-query', 'method': 'worker.turnLease', 'params': params}
    result = asyncio.run(worker.connection.dispatch(request))
    assert result == {'jsonrpc': '2.0', 'id': 'lease-query', 'result': {
        'expires_at': worker.db.session_turn_lease_expires_at('owned', 'holder')}}
    refused = asyncio.run(worker.connection.dispatch(dict(request, params=dict(params, sequence=1))))
    assert refused['error']['data']['reason'] == 'invalid_params'


def test_query_sql_has_no_writes_and_does_not_change_revision(worker, monkeypatch):
    import hermes_state_runtime as runtime
    store, db = worker.store, worker.db
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    revision = db.get_session('owned')['runtime_revision']
    epoch = runtime._epoch
    statements = []

    def trace_epoch(conn, value):
        conn.set_trace_callback(statements.append)
        return epoch(conn, value)

    monkeypatch.setattr(runtime, '_epoch', trace_epoch)
    assert store.session_turn_lease_expires_at('owned', 'holder') is not None
    assert statements
    assert all(sql.lstrip().split()[0].upper() in {'SELECT', 'ROLLBACK'} for sql in statements)
    assert statements[-1] == 'ROLLBACK'
    assert not any('worker_receipts' in sql.lower() for sql in statements)
    assert db.get_session('owned')['runtime_revision'] == revision


def test_store_query_holds_scope_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    held = []
    scope = {'session_id': 'owned', 'epoch': 7}

    @contextmanager
    def scope_lock():
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    def rpc(method, **params):
        assert held == [True]
        assert method == 'worker.turnLease'
        assert params == dict(scope, holder='holder')
        return {'expires_at': 123.0}

    store = RuntimeSessionStore(rpc, scope, tmp_path / 'private')
    monkeypatch.setattr(store, 'lock', scope_lock())
    assert store.session_turn_lease_expires_at('owned', 'holder') == 123.0
    assert held == []
