"""The worker's receipt bound is derived from, and longer than, every owner write patience a
``worker.*`` RPC can spend.

A worker that gives up first kills its run with ``worker_receipt_timeout`` while the owner is still
allowed to wait for the state.db write lock, and may then commit the write anyway (outcome unknown,
never resent). David-MBP lost three runs that way when the bound was a literal 20 s and the owner's
transcript patience was 60 s.
"""
import threading
from types import SimpleNamespace


def _owner_patience_spent(tmp_path):
    """Every ``patience_s`` the owner's real register / adopt / persist handlers pass to the write path."""
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_state import SessionDB
    from hermes_state_runtime import (
        adopt_worker_execution, begin_runtime_epoch, mutate_worker_execution, register_worker_execution,
    )
    db = SessionDB(tmp_path / 'state.db')
    spent, real = [], db._execute_write

    def spy(fn, patience_s=None):
        spent.append(db._WRITE_PATIENCE_S if patience_s is None else patience_s)
        return real(fn, patience_s=patience_s)
    try:
        db.create_session('owned', 'cli', system_prompt='prefix')
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
        db._execute_write = spy
        register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
        adopt_worker_execution(db, **scope, adoption_secret='secret')
        store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
        try:
            store.append_messages_batch('owned', [{'role': 'assistant', 'content': 'done', 'finish_reason': 'stop'}])
            store.finish()
        finally:
            store.close()
    finally:
        db._execute_write = real
        db.close()
    return spent


def test_receipt_bound_is_derived_from_the_owner_patience_plus_margin():
    from hermes_state import SessionDB
    from hermes_state_runtime import WORKER_RECEIPT_MARGIN_S, worker_receipt_timeout_s
    longest = max(SessionDB._WRITE_PATIENCE_S, SessionDB._TRANSCRIPT_WRITE_PATIENCE_S)
    assert WORKER_RECEIPT_MARGIN_S == 30.0
    assert worker_receipt_timeout_s() == longest + WORKER_RECEIPT_MARGIN_S


def test_worker_receipt_bound_outlasts_every_owner_write_patience(tmp_path, monkeypatch):
    from agent import runtime_session_store
    from agent.runtime_session_store import WorkerRPC

    spent = _owner_patience_spent(tmp_path)
    # register + adopt (default patience) and at least two persist receipts (transcript patience).
    assert len(spent) >= 4, spent

    waited = []

    class Link:
        reader = SimpleNamespace(is_alive=lambda: True)

        def send(self, request_id, message):
            self.request_id = request_id

        def receive(self, timeout):
            waited.append(timeout)
            return {'jsonrpc': '2.0', 'id': self.request_id, 'result': {}}
    rpc = WorkerRPC(tmp_path)
    rpc.link = Link()
    for method in ('worker.register', 'worker.adopt', 'worker.persist'):
        rpc(method, pid=-1)
    assert len(waited) == 3
    # The worker must still be listening when the owner's longest write wait ends.
    assert min(waited) > max(spent), (waited, spent)


def test_slow_worker_persist_logs_operation_lock_wait_total_and_session(tmp_path, monkeypatch, caplog):
    """One WARNING per worker.persist slower than the threshold, naming what the owner waited on."""
    import asyncio
    import logging
    from gateway import session_worker
    import sqlite3
    from hermes_state import SessionDB
    from hermes_state_runtime import apply_worker_receipt, begin_runtime_epoch, register_worker_execution
    monkeypatch.setattr(session_worker, 'SLOW_WORKER_PERSIST_S', 0.2)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('owned', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
        holder = sqlite3.connect(str(tmp_path / 'state.db'), isolation_level=None, check_same_thread=False)
        holder.execute('BEGIN IMMEDIATE')
        threading.Timer(0.5, holder.rollback).start()

        def persist():
            return apply_worker_receipt(db, **scope, sequence=1, operation='noop', payload={},
                                        handlers={'noop': lambda db, conn, sid, p: {'ok': True}})
        params = dict(session_id='owned', operation='noop')
        with caplog.at_level(logging.WARNING, logger='gateway.session_worker'):
            result = asyncio.run(session_worker.timed_persist(params, lambda: asyncio.to_thread(persist)))
        holder.close()
        assert result == {'ok': True}
        slow = [r for r in caplog.records if 'slow worker.persist' in r.getMessage()]
        assert len(slow) == 1, [r.getMessage() for r in caplog.records]
        record = slow[0]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert 'operation=noop' in message and 'session=owned' in message, message
        fields = dict(part.split('=', 1) for part in message.split() if '=' in part)
        lock_wait, total = float(fields['lock_wait'].rstrip('s')), float(fields['total'].rstrip('s'))
        assert 0.4 <= lock_wait <= total, message
        assert fields['outcome'] == 'ok', message
    finally:
        db.close()


def test_fast_worker_persist_logs_nothing(tmp_path, caplog):
    import asyncio
    import logging
    from gateway import session_worker

    async def quick():
        return {'ok': True}
    with caplog.at_level(logging.WARNING, logger='gateway.session_worker'):
        assert asyncio.run(session_worker.timed_persist(
            {'session_id': 's', 'operation': 'transcript.append'}, quick)) == {'ok': True}
    assert not [r for r in caplog.records if 'slow worker.persist' in r.getMessage()]
