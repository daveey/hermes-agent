"""Budget/cleanup controls for optional owner observations, not persistence."""
import threading
import time

import pytest

from agent import origin_observation_budget as budget
from agent.runtime_session_store import WorkerPersistenceError, WorkerRPC
from tests.hermes_cli.test_origin_record_rpc_contention import socket_rpc


@pytest.mark.parametrize('which', ['store', 'rpc'])
def test_contended_lock_consumes_budget(tmp_path, monkeypatch, which):
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 0.05)
    rpc = WorkerRPC(tmp_path)
    lock = threading.RLock() if which == 'store' else rpc.lock
    entered, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            entered.set()
            assert release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(3)

    @budget.bounded_record
    def observe():
        if which == 'store':
            with budget.observation_lock(lock):
                pytest.fail('acquired held store lock')
        else:
            rpc('worker.nativeOrigin')

    try:
        started = time.monotonic()
        assert observe() is None
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        thread.join(3)
    assert budget.remaining() is None
    assert not thread.is_alive()


def test_cold_link_never_discovers(tmp_path, monkeypatch):
    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', lambda: pytest.fail('discovery under board lock'))

    @budget.bounded_record
    def observe():
        rpc('worker.turnLease')

    with pytest.raises(WorkerPersistenceError, match='origin_owner_link_cold'):
        observe()
    assert budget.remaining() is None


def test_shared_budget_and_next_persist_has_own_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 0.12)
    calls = []

    def dispatch(method, **params):
        calls.append(method)
        return {'kind': method, '_receipt': {'execution_id': 'exec', 'sequence': 1}} if method == 'worker.persist' else {'kind': method}

    def delay(request):
        time.sleep(0.08)

    with socket_rpc(tmp_path, dispatch, delay, expect_disconnect=True) as rpc:
        @budget.bounded_record
        def observe():
            for _ in range(4):
                rpc('worker.nativeOrigin')

        started = time.monotonic()
        with pytest.raises(WorkerPersistenceError, match='origin_observation_unavailable'):
            observe()
        assert time.monotonic() - started < 0.3
        assert calls == ['worker.nativeOrigin'] * 2
        assert rpc.link is None
        assert budget.remaining() is None
        # Reconnect through a real socket after timeout. The old late reply queue
        # must not answer this write, and the observation deadline is gone.
        with socket_rpc(tmp_path, dispatch, delay) as fresh:
            monkeypatch.setattr(rpc, '_connection', lambda: fresh.link)
            result = rpc('worker.persist', execution_id='exec', sequence=1)
            assert result == {'kind': 'worker.persist'}
            assert calls[-1] == 'worker.persist'
