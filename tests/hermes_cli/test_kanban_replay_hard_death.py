"""SIGKILL at real production HTTP boundaries; fixture peer, not Board deployment."""
import json
import multiprocessing
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


def _push(path, url):
    conn = kbc.connect(path)
    hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
    try:
        client.push(conn, hub, 'default', 'macbook', primary=False, max_batches=1)
    finally:
        hub.close()
        conn.close()


class CrashPeer:
    def __init__(self, stage):
        self.receiver = sender.Receiver()
        self.stage = stage
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
                size = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(size)) if size else None
                response = peer.receiver._call(method, self.path, body)
                if self.path.endswith('/' + str(peer.stage)):
                    peer.entered.set()
                    assert peer.release.wait(30), 'parent did not reap killed sender'
                    # The original sender has died, so do not write to its dead socket.
                    self.close_connection = True
                    return
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)
        assert not self.thread.is_alive()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stage', ['capability', 'session', 'ops', 'ack'])
def test_hard_death_reopens_exact_custody_and_never_resends_accepted_operation(live, stage):
    task = kb.create_task(live, title='survive hard death', assignee='worker')
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    remaining = [tuple(row) for row in live.execute(
        "SELECT op_id,wire_json,exposure_history FROM kanban_sync_outbox WHERE table_name!='tasks'")]
    peer = CrashPeer(stage)
    ctx = multiprocessing.get_context('spawn')
    proc = ctx.Process(target=_push, args=(path, peer.url))
    proc.start()
    try:
        assert peer.entered.wait(30), 'sender did not reach controlled HTTP cut'
        assert proc.is_alive()
        before = journal.snapshot(live)
        frozen = journal.pending_wire(live)
        sources = [tuple(row) for row in live.execute("SELECT op_id,wire_json,exposure_history FROM kanban_sync_outbox WHERE table_name='tasks'")]
        if stage in ('capability', 'session'):
            assert frozen is None and before['settled'] == '0'
            assert len(sources) == 1 and not peer.receiver.sent
        elif stage == 'ops':
            assert frozen is not None and before['settled'] == '0'
            assert len(peer.receiver.sent) == len(sources) == 1
            history = json.loads(sources[0][2])
            assert len(history['exposures']) == 1
            assert history['exposures'][0]['wire'] == peer.receiver.sent[0]
            assert peer.receiver.slot['h'] == '1' and peer.receiver.slot['a'] == '0'
        else:
            assert frozen is None and before['settled'] == '1'
            assert sources == [] and len(peer.receiver.sent) == 1
            assert peer.receiver.slot['h'] == peer.receiver.slot['a'] == '1'
        proc.kill()
        proc.join(10)
        assert proc.exitcode == -getattr(signal, "SIGKILL", signal.SIGTERM)
        peer.stage = None
        peer.release.set()
        reopened = kbc.connect(path)
        try:
            assert journal.snapshot(reopened) == before
            assert journal.pending_wire(reopened) == frozen
            assert [tuple(row) for row in reopened.execute(
                "SELECT op_id,wire_json,exposure_history FROM kanban_sync_outbox WHERE table_name='tasks'")] == sources
            hub = client.Hub(client.Identity(peer.url, 'fixture-key', 'macbook'))
            try:
                result = client.push(reopened, hub, 'default', 'macbook', primary=False,
                                     max_batches=0 if stage == 'ack' else 1)
            finally:
                hub.close()
            assert result.applied == (0 if stage == 'ack' else 1)
            assert len(peer.receiver.sent) == 1  # Actual receiver commits once across process death.
            assert journal.pending_wire(reopened) is None
            assert journal.snapshot(reopened)['settled'] == peer.receiver.slot['a'] == '1'
            assert [tuple(row) for row in reopened.execute(
                'SELECT op_id,wire_json,exposure_history FROM kanban_sync_outbox')] == remaining
            assert reopened.execute('SELECT sync_version FROM tasks WHERE id=?', (task,)).fetchone()[0] == 7
            # The independent lifecycle event progresses, not a resend of the task.
            original = peer.receiver.sent[0]['client_op_id']
            next_result = client.push(reopened, peer.receiver, 'default', 'macbook', primary=False, max_batches=1)
            assert next_result.applied == 1
            assert len(peer.receiver.sent) == 2
            assert sum(op['client_op_id'] == original for op in peer.receiver.sent) == 1
            assert peer.receiver.sent[1]['client_op_id'] == remaining[0][0]
        finally:
            reopened.close()
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        peer.close()
