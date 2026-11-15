"""Native-origin enrichment is opt-in, owner-authenticated and fail-closed."""
import asyncio
import copy
import json

import pytest

from tests.gateway import test_worker_turn_lease_query as lease_tests

worker = lease_tests.worker


def _compress(store, parent, child):
    assert store.try_acquire_compression_lock(parent, 'compressor')
    store.publish_compression_child(parent_session_id=parent, child_session_id=child, source='cli',
        messages=[{'role': 'assistant', 'content': 'summary'}], compression_lock_holder='compressor')


def test_native_origin_real_dispatch_and_store_transport(worker):
    store, db = worker.store, worker.db
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    journal, disk = copy.deepcopy(store.journal), store.path.read_bytes()
    sequence = db._read_one('SELECT last_sequence FROM worker_executions')[0]
    params = dict(store.scope, holder='holder')
    request = {'jsonrpc': '2.0', 'id': 'native-query', 'method': 'worker.nativeOrigin', 'params': params}
    expected = {'expires_at': db.session_turn_lease_expires_at('owned', 'holder'),
                'source': 'cli', 'assignment_lineage': ['owned']}
    assert asyncio.run(worker.connection.dispatch(request)) == {
        'jsonrpc': '2.0', 'id': 'native-query', 'result': expected}
    assert store.native_origin_observation('owned', 'holder') == expected
    refused = asyncio.run(worker.connection.dispatch(dict(request, params=dict(params, sequence=1))))
    assert refused['error']['data']['reason'] == 'invalid_params'
    assert store.native_origin_observation('owned', 'foreign-holder') == dict(expected, expires_at=None)
    assert store.journal == journal and store.path.read_bytes() == disk
    assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == sequence


def test_ordinary_query_never_calls_lineage_validation(worker, monkeypatch):
    import hermes_state_runtime as runtime
    assert worker.store.try_acquire_session_turn_lease('owned', 'holder')

    def refuse(*args):
        raise runtime.RuntimeStoreError('permission_denied')

    monkeypatch.setattr(runtime, '_worker_publication_lineage', refuse)
    expected = {'expires_at': worker.db.session_turn_lease_expires_at('owned', 'holder')}
    assert worker.rpc('worker.turnLease', **worker.store.scope, holder='holder') == expected
    with pytest.raises(runtime.RuntimeStoreError, match='^permission_denied$'):
        worker.store.native_origin_observation('owned', 'holder')


def test_broken_publication_chain_does_not_break_ordinary_lease(worker):
    from hermes_state_runtime import RuntimeStoreError
    store, db = worker.store, worker.db
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    expiry = store.session_turn_lease_expires_at('owned', 'holder')
    _compress(store, 'owned', 'child')
    _compress(store, 'child', 'tip')
    assert store.native_origin_observation('tip', 'holder')['assignment_lineage'] == ['owned', 'child', 'tip']
    rows = db._read_all("SELECT sequence,result_json FROM worker_receipts "
                        "WHERE json_extract(result_json,'$.worker_assignment.parent') IS NOT NULL ORDER BY sequence")
    receipt = json.loads(rows[-1]['result_json'])
    receipt['worker_assignment']['parent'] = 'unrelated'
    db._write_sql('UPDATE worker_receipts SET result_json=? WHERE sequence=?',
                  (json.dumps(receipt), rows[-1]['sequence']))
    with pytest.raises(RuntimeStoreError, match='^permission_denied$'):
        store.native_origin_observation('tip', 'holder')
    assert worker.rpc('worker.turnLease', **store.scope, holder='holder') == {'expires_at': expiry}


@pytest.mark.parametrize(('root', 'current', 'expected'), [
    (('cli', None), ('cli', None), 'cli'),
    (('local', 'desktop'), ('desktop', None), 'desktop'),
    (('local', 'kanban'), ('cli', None), None),
    (('cli', None), ('local', 'cron'), None),
    (('cli', 'kanban'), ('cli', 'kanban'), 'kanban'),
    (('desktop', None), ('cli', None), None),
])
def test_root_and_current_creation_classification_must_agree(worker, root, current, expected):
    store, db = worker.store, worker.db
    assert store.try_acquire_session_turn_lease('owned', 'holder')
    expiry = store.session_turn_lease_expires_at('owned', 'holder')
    _compress(store, 'owned', 'child')
    db._write_sql('UPDATE sessions SET source=?,created_source=? WHERE id=?', (*root, 'owned'))
    db._write_sql('UPDATE sessions SET source=?,created_source=? WHERE id=?', (*current, 'child'))
    assert store.native_origin_observation('child', 'holder') == {
        'expires_at': expiry, 'source': expected, 'assignment_lineage': ['owned', 'child']}
    assert store.session_turn_lease_expires_at('child', 'holder') == expiry
