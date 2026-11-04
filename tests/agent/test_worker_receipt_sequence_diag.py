"""A skipped worker receipt sequence names itself on both sides of the worker boundary.

Run 36008 (t_01a1a264e0987946) and its sibling worker died at the same second with a sticky
``WorkerPersistenceError: invalid_params``: the worker journal held sequence 194 while the owner's
``worker_executions.last_sequence`` was 192. The owner raised a bare code, which the gateway logs at
debug only, and the worker raised the bare code, so no log line said which check fired or which
write was missing. The refusal now carries ``field='sequence'`` and ``expected N, got M``; the wire
reason stays ``invalid_params`` (callers compare it), and the worker logs the named check.
"""
import json
import logging
import queue
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed

import hermes_state_runtime as rt
from tests.hermes_state.test_runtime_worker import append, setup_worker


def test_owner_names_the_expected_sequence(tmp_path):
    db, epoch, assignment = setup_worker(tmp_path)
    try:
        append(db, epoch=epoch, **assignment, sequence=1, content='first')
        with pytest.raises(rt.RuntimeStoreError) as caught:
            append(db, epoch=epoch, **assignment, sequence=3, content='skipped two')
        assert caught.value.reason == 'invalid_params'
        assert caught.value.field == 'sequence'
        assert caught.value.detail == 'expected 2, got 3'
        assert db._conn.execute(
            "SELECT last_sequence FROM worker_executions WHERE execution_id='worker'").fetchone()[0] == 1
    finally:
        db.close()


def test_gateway_logs_named_sequence_refusal_at_warning(caplog):
    from gateway.session_controls import _log_refusal, _refusal_data
    exc = rt.RuntimeStoreError('invalid_params', field='sequence', detail='expected 193, got 194')
    with caplog.at_level(logging.DEBUG, logger='gateway.session_controls'):
        _log_refusal('/home/x/.hermes', 'worker.persist', exc)
    record = next(r for r in caplog.records if 'worker.persist refused' in r.getMessage())
    assert record.levelno == logging.WARNING
    assert 'field=sequence detail=expected 193, got 194' in record.getMessage()
    assert _refusal_data(exc) == {'reason': 'invalid_params', 'field': 'sequence',
                                  'detail': 'expected 193, got 194'}


def _owner(monkeypatch, reply):
    endpoint = SimpleNamespace(api_origin='http://127.0.0.1:1234')
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint',
                        lambda *a, **k: SimpleNamespace(state='ready', endpoint=endpoint))
    monkeypatch.setattr('hermes_cli.gateway_runtime.control_home_for', lambda home, endpoint: home)
    monkeypatch.setattr('hermes_cli.gateway_runtime_discovery.query_identify', lambda *a, **k: {'pid': -1})
    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket', lambda *a, **k: 'ticket')

    def connect(url, **kwargs):
        frames = queue.SimpleQueue()

        def recv():
            frame = frames.get()
            if frame is None:
                raise ConnectionClosed(None, None)
            return frame

        def send(value):
            frames.put(json.dumps({'id': json.loads(value)['id'], 'error': reply}))
        return SimpleNamespace(send=send, recv=recv, close=lambda: frames.put(None))
    monkeypatch.setattr('websockets.sync.client.connect', connect)


def test_worker_logs_named_refusal_and_keeps_bare_reason(tmp_path, monkeypatch, caplog):
    from agent.runtime_session_store import WorkerPersistenceError, WorkerRPC
    _owner(monkeypatch, {'code': 4001, 'message': 'invalid_params', 'data': {
        'reason': 'invalid_params', 'field': 'sequence', 'detail': 'expected 193, got 194'}})
    rpc = WorkerRPC(tmp_path)
    with caplog.at_level(logging.WARNING, logger='agent.runtime_session_store'):
        with pytest.raises(WorkerPersistenceError) as caught:
            rpc('worker.persist', operation='session.sidecars', sequence=194)
    assert str(caught.value) == 'invalid_params'
    lines = [r.getMessage() for r in caplog.records if 'worker owner refused' in r.getMessage()]
    assert lines == ['worker owner refused worker.persist operation=session.sidecars sequence=194: '
                     'invalid_params field=sequence detail=expected 193, got 194']
    rpc.close()


def test_worker_bare_refusal_logs_nothing_new(tmp_path, monkeypatch, caplog):
    from agent.runtime_session_store import WorkerPersistenceError, WorkerRPC
    _owner(monkeypatch, {'code': 4001, 'message': 'session_busy', 'data': {'reason': 'session_busy'}})
    rpc = WorkerRPC(tmp_path)
    with caplog.at_level(logging.WARNING, logger='agent.runtime_session_store'):
        with pytest.raises(WorkerPersistenceError, match='session_busy'):
            rpc('worker.persist', operation='turn.acquire', sequence=5)
    assert not [r for r in caplog.records if 'worker owner refused' in r.getMessage()]
    rpc.close()
