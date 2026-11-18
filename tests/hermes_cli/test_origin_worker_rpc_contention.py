"""Real worker facade, owner reads and same-transaction board claim under stall."""
import sqlite3
import threading
import time

import pytest

from tests.hermes_cli import test_origin_worker_custody as worker_tests
from tests.hermes_cli.test_origin_record_rpc_contention import socket_rpc

worker = worker_tests.worker


@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('revoke', [False, True])
def test_worker_record_stall(worker, tmp_path, monkeypatch, bounded, revoke):
    from agent import origin_observation_budget as budget
    from hermes_cli import kanban_db_connect as kbc, kanban_origin_native as native

    agent, store, db, context, _ = worker
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 0.05 if bounded else 2.0)
    calls, contention = [], []
    received, release = threading.Event(), threading.Event()
    original = store.rpc
    from hermes_cli import kanban_db as kb
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title='receipt target', assignee='default')

    def before_reply(request):
        calls.append(request['method'])
        if len(calls) == 1:
            received.set()
            assert release.wait(5)

    def contender():
        assert received.wait(5)
        try:
            with sqlite3.connect(context['db'], timeout=0.2) as conn:
                conn.execute('BEGIN IMMEDIATE')
                contention.append('acquired')
        except sqlite3.OperationalError as exc:
            contention.append(str(exc))
        finally:
            if revoke:
                db._write_sql('UPDATE session_turn_leases SET expires_at=1')
            release.set()

    def loop(*args, **kwargs):
        captured = native.capture(context['task_id'])
        assert captured is not None
        with socket_rpc(tmp_path, original, before_reply, expect_disconnect=bounded) as rpc:
            store.rpc = rpc
            rival = threading.Thread(target=contender)
            rival.start()
            try:
                with kbc.connect_closing() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    started = time.monotonic()
                    native.record(conn, child, captured, 1)
                    elapsed = time.monotonic() - started
                    receipt = native._receipt(conn, child)
                    conn.commit()
                assert (receipt is None) == (bounded or revoke)
                if bounded:
                    assert elapsed < 0.18
                assert store.failure is None
            finally:
                release.set()
                rival.join(5)
                store.rpc = original
            assert not rival.is_alive()
        assert contention == ['acquired' if bounded else 'database is locked']
        assert calls == ['worker.turnLease'] * (1 if bounded else 2 if revoke else 4)
        return dict(final_response='ok', messages=[], failed=False)

    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    assert agent.run_conversation('record under contention')['final_response'] == 'ok'
