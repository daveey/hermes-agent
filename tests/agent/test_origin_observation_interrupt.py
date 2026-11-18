"""Abandoned optional observations must never answer subsequent durable writes."""
import os
import signal
import time

import pytest

from agent import origin_observation_budget as budget
from tests.hermes_cli.test_origin_record_rpc_contention import socket_rpc


@pytest.mark.parametrize('failure', ['sigint', 'system_exit', 'runtime_error'])
@pytest.mark.parametrize('boundary', ['send', 'receive'])
def test_interrupted_observation_cannot_answer_next_write(tmp_path, monkeypatch, failure, boundary):
    calls = []

    def dispatch(method, **params):
        calls.append(method)
        if method == 'worker.persist':
            return {'kind': 'persist', '_receipt': {'execution_id': 'exec', 'sequence': 1}}
        return {'kind': 'observation'}

    # Keep the reply-queued signal probe independent of the deadline timeout.
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 3)
    error = {'sigint': KeyboardInterrupt, 'system_exit': SystemExit, 'runtime_error': RuntimeError}[failure]
    with socket_rpc(tmp_path, dispatch, lambda request: None, expect_disconnect=True) as rpc:
        link = rpc.link
        assert link is not None
        original = getattr(link, boundary)

        def interrupted(*args):
            if boundary == 'send':
                original(*args)
            deadline = time.monotonic() + 1
            while link.replies.empty() and time.monotonic() < deadline:
                time.sleep(0.001)
            assert not link.replies.empty()
            if failure == 'sigint':
                os.kill(os.getpid(), signal.SIGINT)
                pytest.fail('SIGINT failed to interrupt')
            raise error('observation interrupted')

        monkeypatch.setattr(link, boundary, interrupted)

        @budget.bounded_record
        def observe():
            rpc('worker.nativeOrigin')

        with pytest.raises(error):
            observe()
        monkeypatch.setattr(link, boundary, original)
        assert budget.remaining() is None
        assert rpc.link is None
        link.reader.join(1)
        link.ws.recv_events_thread.join(1)
        assert not link.reader.is_alive()
        assert not link.ws.recv_events_thread.is_alive()
        # A real redial must return the write's own receipt, not the queued read.
        with socket_rpc(tmp_path, dispatch, lambda request: None) as fresh:
            monkeypatch.setattr(rpc, '_connection', lambda: rpc.link if rpc.link is not None else fresh.link)
            result = rpc('worker.persist', execution_id='exec', sequence=1)
            assert result == {'kind': 'persist'}
            assert calls == ['worker.nativeOrigin', 'worker.persist']
