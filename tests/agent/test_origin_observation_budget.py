"""Budget/cleanup controls for optional owner observations, not persistence."""
import socket
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


@pytest.mark.parametrize('blocked_send', [False, True])
def test_real_socket_stall_interrupts_send_and_joins_readers(tmp_path, monkeypatch, blocked_send):
    """A nonreading peer exercises websockets' internal send-error join too."""
    from websockets.client import ClientProtocol
    from websockets.protocol import OPEN
    from websockets.sync.client import ClientConnection
    from websockets.uri import parse_uri
    from agent.runtime_session_store import _OwnerLink

    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 0.05)
    ours, peer = socket.socketpair()
    ours.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    ws = ClientConnection(ours, ClientProtocol(parse_uri('ws://localhost/'), state=OPEN),
                          ping_interval=None, close_timeout=10)
    link = _OwnerLink(ws)
    rpc = WorkerRPC(tmp_path)
    rpc.link = link
    sent = []
    send = link.send

    def tracked_send(*args):
        send(*args)
        sent.append(True)

    monkeypatch.setattr(link, 'send', tracked_send)

    @budget.bounded_record
    def observe():
        rpc('worker.turnLease', padding='x' * (1024 * 1024 if blocked_send else 1))

    # Safety release prevents a broken mutant from hanging the entire test run.
    rescue = threading.Timer(2, peer.close)
    rescue.start()
    try:
        started = time.monotonic()
        with pytest.raises(WorkerPersistenceError, match='origin_observation_unavailable'):
            observe()
        assert time.monotonic() - started < 0.4
        assert bool(sent) is not blocked_send
        assert rpc.link is None
        link.reader.join(0.4)
        ws.recv_events_thread.join(0.4)
        assert not link.reader.is_alive()
        assert not ws.recv_events_thread.is_alive()
    finally:
        rescue.cancel()
        rescue.join()
        peer.close()
        ws.close()
        rpc.close()


def test_nested_budget_never_extends_and_resets(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 1.0)

    @budget.bounded_record
    def nested():
        assert budget.remaining() == pytest.approx(0.25)
        clock[0] = 11.1
        budget.remaining()
        pytest.fail('expired budget accepted')

    @budget.bounded_record
    def outer():
        clock[0] = 10.75
        assert nested() is None
        with pytest.raises(TimeoutError):
            budget.remaining()

    outer()
    assert budget.remaining() is None


def test_success_crossing_timer_join_is_rejected(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 1.0)
    from types import SimpleNamespace

    class Timer:
        def __init__(self, *args):
            pass
        def start(self):
            pass
        def cancel(self):
            pass
        def join(self):
            clock[0] = 11.1

    monkeypatch.setattr(budget.threading, 'Timer', Timer)

    @budget.bounded_record
    def observe():
        with budget.interrupt_at_deadline(SimpleNamespace(socket=None)):
            pass
        pytest.fail('late success accepted')

    assert observe() is None
    assert budget.remaining() is None


@pytest.mark.parametrize('dead', [False, True])
def test_cold_link_never_discovers(tmp_path, monkeypatch, dead):
    rpc = WorkerRPC(tmp_path)
    if dead:
        from types import SimpleNamespace
        rpc.link = SimpleNamespace(reader=SimpleNamespace(is_alive=lambda: False))
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
        time.sleep(0.08)  # health: allow HX013 -- per-reply latency must exhaust the shared RPC budget

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
