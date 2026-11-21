"""Focused RPC error-code and retry tests for fresh session observations."""
from contextlib import contextmanager

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError, WorkerRPC


@pytest.mark.parametrize('code', [-32601, 4001, -32603])
def test_rpc_preserves_exact_wire_error_code(tmp_path, monkeypatch, code):
    class Link:
        def send(self, request_id, message):
            pass

        def receive(self, timeout):
            return {'error': {'code': code, 'message': 'Method not found'}}

    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', Link)
    with pytest.raises(WorkerPersistenceError) as caught:
        rpc('worker.sessionContext')
    assert caught.value.code == code


def test_context_query_holds_scope_lock(tmp_path, monkeypatch):
    held = []

    @contextmanager
    def lock():
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    def rpc(method, **params):
        assert held == [True]
        assert method == 'worker.sessionContext'
        assert params == {'session_id': 'owned', 'epoch': 7, 'target': 'owned'}
        return {'session': {'id': 'owned'}}

    store = RuntimeSessionStore(rpc, {'session_id': 'owned', 'epoch': 7}, tmp_path / 'outbox')
    monkeypatch.setattr(store, 'lock', lock())
    assert store.get_session('owned') == {'id': 'owned'}
    assert held == []
    # Restore before closing; the fixture lock is deliberately single-use.
    import threading
    store.lock = threading.RLock()
    store.close()


@pytest.mark.parametrize('kind', ['timeout', 'closed', 'connection', 'other_timeout'])
def test_context_failed_read_never_falls_back_to_a_write(tmp_path, kind):
    from websockets.exceptions import ConnectionClosedError
    errors = {'timeout': TimeoutError('worker_receipt_timeout'),
              'closed': ConnectionClosedError(None, None),
              'connection': ConnectionError('closed'), 'other_timeout': TimeoutError('unrelated')}
    error = errors[kind]
    calls = []

    def rpc(method, **params):
        calls.append(method)
        raise error

    store = RuntimeSessionStore(rpc, {'session_id': 'owned'}, tmp_path / 'outbox')
    before = store.path.read_bytes()
    try:
        with pytest.raises(type(error)) as caught:
            store.get_session('owned')
        assert caught.value is error
        assert calls == ['worker.sessionContext'] * (1 if kind == 'other_timeout' else 2)
        assert store.path.read_bytes() == before
        assert store.failure is None
        assert store.journal['pending'] == []
    finally:
        store.close()
