"""Crash and fence coverage for startup metadata receipt-clear coalescing."""
import json

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from tests.hermes_state.test_runtime_worker_lifecycle import setup_store


@pytest.mark.parametrize('crash_point', ['after_ack', 'before_next_rpc', 'after_next_ack'])
def test_metadata_receipts_replay_exactly_across_crash(tmp_path, crash_point):
    db, store, scope = setup_store(tmp_path)
    original = store.rpc
    recovered = None
    seen = []
    def rpc(method, **params):
        seen.append(params['sequence'])
        return original(method, **params)
    store.rpc = rpc
    try:
        store.update_system_prompt('owned', 'first')
        assert json.loads(store.path.read_text())['pending'][0]['sequence'] == 1
        if crash_point == 'before_next_rpc':
            def crash(method, **params):
                raise RuntimeError('simulated process loss before send')
            store.rpc = crash
            with pytest.raises(RuntimeError, match='simulated process loss'):
                store.update_system_prompt('owned', 'second')
        elif crash_point == 'after_next_ack':
            store.update_system_prompt('owned', 'second')
        expected = 1 if crash_point == 'after_ack' else 2
        journal = json.loads(store.path.read_text())
        assert journal['pending'][0]['sequence'] == expected
        store._outbox_owner.close()  # simulate process loss: deliberately do NOT call close/flush
        recovered = RuntimeSessionStore(rpc, scope, store.path.parent)
        assert seen[-1] == expected
        assert db.get_session('owned')['system_prompt'] == ('first' if expected == 1 else 'second')
        assert recovered.journal['next_sequence'] == expected + 1
        recovered.update_system_prompt('owned', 'third')
        assert seen[-1] == expected + 1
        assert db.get_session('owned')['system_prompt'] == 'third'
    finally:
        if recovered:
            recovered.close()
        else:
            store.close()
        db.close()


def test_next_metadata_write_still_rejects_changed_generation(tmp_path):
    db, store, _scope = setup_store(tmp_path)
    try:
        store.update_system_prompt('owned', 'first')
        db._execute_write(lambda conn: conn.execute(
            'UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id=?', ('owned',)))
        with pytest.raises(WorkerPersistenceError, match='stale_generation'):
            store.update_system_prompt('owned', 'forbidden')
        assert db.get_session('owned')['system_prompt'] == 'first'
        assert json.loads(store.path.read_text())['pending'][0]['sequence'] == 2
    finally:
        store._outbox_owner.close()
        db.close()
