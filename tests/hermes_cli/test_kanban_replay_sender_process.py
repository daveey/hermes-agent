"""Independent-process sender occupancy through a real HTTP session. [daveey]"""
import json
import multiprocessing
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync, kanban_sync_client as client
from hermes_cli.kanban_sync_guard import SyncRoundBusy, board_sync_lock
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


def _send(path, url, results):
    conn = kbc.connect(path)
    try:
        hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
        result = client.push(conn, hub, 'default', 'macbook', primary=False, max_batches=1)
        results.put(('ok', result.applied))
    except (OSError, ValueError, RuntimeError, client.HubError) as exc:
        results.put(('error', str(exc)))
        raise
    finally:
        conn.close()


class Peer:
    def __init__(self, stage):
        self.receiver = helpers.Receiver()
        self.entered = threading.Event()
        self.release = threading.Event()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply('GET')

            def do_POST(self):
                self.reply('POST')

            def reply(self, method):
                assert self.headers.get('Authorization') == 'Bearer fixture-key'
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length)) if length else None
                data = json.dumps(peer.receiver._call(method, self.path, body)).encode()
                if self.path.endswith('/' + stage):
                    peer.entered.set()
                    assert peer.release.wait(30), 'parent did not release real HTTP response'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)
        assert not self.thread.is_alive()


@pytest.mark.parametrize('action', ['push', 'detach'])
@pytest.mark.parametrize('stage', ['session', 'ops'])
def test_process_in_http_session_excludes_other_sender_and_detach(live, action, stage, monkeypatch):
    from hermes_cli import kanban_replay_sender
    monkeypatch.setattr(kanban_replay_sender, 'DIRECT_WAIT_S', 0)
    kb.create_task(live, title='one sender', assignee='worker')
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    peer = Peer(stage)
    ctx = multiprocessing.get_context('spawn')
    results = ctx.Queue()
    proc = ctx.Process(target=_send, args=(path, f'http://127.0.0.1:{peer.server.server_port}', results))
    proc.start()
    other = helpers.Receiver()
    try:
        assert peer.entered.wait(30), 'child never reached the real HTTP boundary'
        assert proc.is_alive()
        before = list(live.iterdump())
        with pytest.raises(SyncRoundBusy):
            if action == 'push':
                client.push(live, other, 'default', 'macbook', primary=False, max_batches=1)
            else:
                sync.set_mode(live, 'off')
        assert other.calls == []
        assert list(live.iterdump()) == before
        peer.release.set()
        proc.join(30)
        assert proc.exitcode == 0
        assert results.get(timeout=5) == ('ok', 1)
        assert len(peer.receiver.sent) == 1
        # The normal round already holds its distinct round lock. No recursive
        # lock or bypass token is needed to enter the sender after drainage.
        with board_sync_lock(path):
            client.push(live, peer.receiver, 'default', 'macbook', primary=False, max_batches=1)
        sync.set_mode(live, 'off')
    finally:
        peer.release.set()
        if proc.is_alive():
            proc.terminate()
        proc.join(10)
        results.close()
        results.join_thread()
        peer.close()


def test_detached_preopened_sender_does_not_publish(live):
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    preopened = kbc.connect(path)
    receiver = helpers.Receiver()
    try:
        sync.set_mode(live, 'off')
        before = list(live.iterdump())
        with pytest.raises(ValueError, match='detached'):
            client.push(preopened, receiver, 'default', 'macbook', primary=False)
        assert receiver.calls == []
        assert list(live.iterdump()) == before
    finally:
        preopened.close()


def test_sender_refuses_nonpersistent_board_before_any_io():
    import sqlite3
    conn = sqlite3.connect(':memory:')
    receiver = helpers.Receiver()
    try:
        with pytest.raises(ValueError, match='on-disk board'):
            client.push(conn, receiver, 'default', 'macbook', primary=False)
        assert receiver.calls == []
    finally:
        conn.close()
