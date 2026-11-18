"""Real socket/board contention at native receipt recording (isolated owners only)."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from tests.agent import test_origin_managed_custody as managed_tests

worker = managed_tests.worker
managed = managed_tests.managed


@contextmanager
def socket_rpc(home, dispatch, before_reply, *, expect_disconnect=False):
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
    from websockets.sync.server import serve
    from agent.runtime_session_store import WorkerRPC, _OwnerLink

    errors = []

    def handler(ws):
        try:
            for raw in ws:
                request = json.loads(raw)
                result = dispatch(request['method'], **request['params'])
                before_reply(request)
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}))
        except (OSError, ValueError, ConnectionClosed) as exc:
            errors.append(exc)

    with serve(handler, '127.0.0.1', 0) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        rpc = WorkerRPC(home)
        rpc.link = _OwnerLink(connect(f'ws://127.0.0.1:{server.socket.getsockname()[1]}',
                                     proxy=None, ping_interval=None, close_timeout=0.1))
        try:
            yield rpc
        finally:
            rpc.close()
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert not errors or expect_disconnect


@pytest.mark.parametrize('revoke', [False, True])
@pytest.mark.parametrize('bounded', [False, True])
def test_record_owner_stall_holds_board_writer(managed, tmp_path, monkeypatch, revoke, bounded):
    """Controlled stall, not evidence of a historical production incident."""
    from hermes_cli import kanban_origin_native as native
    from agent import origin_observation_budget as budget
    monkeypatch.setattr(budget, 'RECORD_TIMEOUT', 0.05 if bounded else 2.0)

    path = tmp_path / 'board.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, created_on TEXT, created_at INTEGER)')
        conn.execute('CREATE TABLE kanban_sync_state (board_id TEXT, gateway_id TEXT, mode TEXT)')
        conn.execute("INSERT INTO tasks VALUES ('t_new', 'here', 1)")
    monkeypatch.setattr(native, '_primary_scope', lambda conn: ('here', 'default', 'default'))
    agent = managed_tests._agent(managed.store)
    received = threading.Event()
    release = threading.Event()
    contention = []
    calls = []

    def before_reply(request):
        calls.append(request['method'])
        if len(calls) == 1:
            received.set()
            assert release.wait(5)

    def contender():
        assert received.wait(5)
        started = time.monotonic()
        try:
            with sqlite3.connect(path, timeout=0.2) as other:
                other.execute('BEGIN IMMEDIATE')
                contention.append(('acquired', time.monotonic() - started))
        except sqlite3.OperationalError as exc:
            contention.append((str(exc), time.monotonic() - started))
        finally:
            if revoke:
                managed.db._write_sql('UPDATE session_turn_leases SET expires_at=1')
            release.set()

    def inside():
        captured = native.capture(None)
        assert captured is not None
        with socket_rpc(tmp_path, managed.rpc, before_reply, expect_disconnect=bounded) as rpc:
            original = managed.store.rpc
            managed.store.rpc = rpc
            rival = threading.Thread(target=contender)
            rival.start()
            try:
                with sqlite3.connect(path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    started = time.monotonic()
                    native.record(conn, 't_new', captured, 1)
                    elapsed = time.monotonic() - started
                    receipt = native._receipt(conn, 't_new')
                assert (receipt is None) == (revoke or bounded)
                assert managed.store.failure is None
                if bounded:
                    assert rpc.link is None  # late replies cannot become persistence receipts
                    assert elapsed < 0.18
            finally:
                release.set()
                rival.join(timeout=5)
                managed.store.rpc = original
            assert not rival.is_alive()
        assert contention and contention[0][0] == ('acquired' if bounded else 'database is locked')
        if not bounded:
            assert elapsed >= contention[0][1]
        assert calls == ['worker.nativeOrigin'] * (1 if bounded else 2 if revoke else 4)
        # Once recording returns, the independently opened writer progresses.
        with sqlite3.connect(path, timeout=0.2) as other:
            other.execute('BEGIN IMMEDIATE')
            other.execute("UPDATE tasks SET created_at=2 WHERE id='t_new'")
        print(json.dumps({'revoke': revoke, 'record_seconds': elapsed,
                          'competing_writer_seconds': contention[0][1], 'rpc_calls': len(calls)}))

    managed_tests._turn(agent, monkeypatch, inside)
