"""Production HTTP request budget against a loopback fixture, not Board proof."""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli.test_kanban_replay_ack_carrier import AckReceiver
from tests.hermes_cli.test_kanban_replay_exposure import history
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


@contextmanager
def serving(conn, receiver, *, before_reply=None, lose_ops_reply=False):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    requests = []
    observations = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            self.reply('GET')

        def do_POST(self):
            self.reply('POST')

        def reply(self, method):
            try:
                auth = self.headers.get('Authorization')
                requests.append((method, self.path, auth, self.client_address))
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length)) if length else None
                if self.path.endswith('/ops'):
                    with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as observed:
                        observed.row_factory = sqlite3.Row
                        for op in body['ops']:
                            saved = history(observed, op['client_op_id'])
                            assert saved is not None, 'HTTP body preceded exposure commit'
                            assert saved['exposures'], 'HTTP body preceded durable exposure entry'
                            entry = saved['exposures'][-1]
                            assert entry['wire'] == op
                            assert entry['credential_epoch'] == hashlib.sha256(b'fixture-key').hexdigest()
                            assert entry['gateway_principal'] == receiver.slot['gateway_principal']
                            assert entry['board_incarnation'] == receiver.slot['board_incarnation']
                            assert auth == 'Bearer fixture-key'
                            observations.append(saved)
                reply = receiver._call(method, self.path, body)
                if lose_ops_reply and self.path.endswith('/ops'):
                    self.close_connection = True
                    return
                if before_reply is not None:
                    before_reply(self.path)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                errors.append(exc)
                self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests, observations
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        assert not thread.is_alive()
        assert not errors, errors


def test_http_backlog_has_one_capability_probe_and_one_roundtrip_per_mutation(live):
    for n in range(20):
        kb.create_task(live, title=f'HTTP backlog {n}', assignee='worker')
    receiver = AckReceiver()
    with serving(live, receiver) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            result = client.push(live, hub, 'default', 'macbook', primary=False, max_batches=20)
        finally:
            hub.close()
    assert result.sent == result.applied == 20
    actions = [path.rsplit('/', 1)[-1] for _, path, _, _ in requests]
    assert actions == ['capability', 'inspect', 'session'] + ['ops'] * 20 + ['ack']
    assert len({address for _, _, _, address in requests}) == 1, 'session did not reuse its HTTP connection'
    assert len(observations) == 20
    assert all(item['certified'] is False for item in observations)
    assert journal.snapshot(live)['settled'] == receiver.slot['a'] == '20'
    assert journal.pending_wire(live) is None


def test_outer_credential_replacement_during_probe_cannot_change_session_headers(live):
    kb.create_task(live, title='immutable session credentials', assignee='worker')
    receiver = AckReceiver()
    hub = None

    def replace(path):
        if path.endswith('/capability'):
            hub.ident = client.Identity(url, 'replacement-key', 'replacement-display')

    with serving(live, receiver, before_reply=replace) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            assert client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1).applied == 1
        finally:
            hub.close()
    assert {auth for _, _, auth, _ in requests} == {'Bearer fixture-key'}
    assert len(observations) == 1


def test_capability_refusal_never_opens_session_or_sends_mutation(live):
    kb.create_task(live, title='unavailable capability', assignee='worker')
    receiver = AckReceiver()
    receiver.supported = False
    with serving(live, receiver) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with pytest.raises(ValueError, match='replay capability unavailable'):
                client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
        finally:
            hub.close()
    assert len(requests) == 1 and observations == []
    assert journal.pending_wire(live) is None
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] > 0


def test_each_public_turn_reprobes_and_uses_a_new_pinned_connection(live):
    receiver = AckReceiver()
    with serving(live, receiver) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            for n in range(2):
                kb.create_task(live, title=f'separate turn {n}', assignee='worker')
                assert client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1).applied == 1
        finally:
            hub.close()
    assert receiver.calls == ['capability', 'inspect', 'session', 'ops', 'ack'] * 2
    assert len({address for _, _, _, address in requests}) == 2
    assert len(observations) == 2


def test_actual_http_reply_loss_reopen_recovers_without_resend(live):
    from pathlib import Path
    from hermes_cli import kanban_db_connect as kbc
    kb.create_task(live, title='accepted then disconnected', assignee='worker')
    receiver = AckReceiver()
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    with serving(live, receiver, lose_ops_reply=True) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with pytest.raises(OSError):
                client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
            pending = journal.pending_wire(live)
            assert pending is not None and journal.snapshot(live)['settled'] == '0'
            with kbc.connect(path) as reopened:
                assert journal.pending_wire(reopened) == pending
                assert client.push(reopened, hub, 'default', 'macbook', primary=False, max_batches=1).applied == 1
                assert journal.pending_wire(reopened) is None
                assert journal.snapshot(reopened)['settled'] == receiver.slot['a'] == '1'
        finally:
            hub.close()
    assert len(receiver.sent) == len(observations) == 1
    assert [p for _, p, _, _ in requests if p.endswith('/ops')] == ['/v2/boards/default/replay/ops']
    assert receiver.calls.count('capability') == 2


@pytest.mark.parametrize('uncommitted', [False, True])
def test_pinned_session_refuses_mutation_without_durable_exposure(live, uncommitted):
    from hermes_cli.kanban_sync_exposure import replay_session
    tid = kb.create_task(live, title='exposure commit refusal', assignee='worker')
    row = live.execute('SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone()
    receiver = AckReceiver()
    path = live.execute('PRAGMA database_list').fetchone()[2]
    with serving(live, receiver) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with replay_session(hub, 'default') as (_transport, _capability, send_ops):
                with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as readonly:
                    if uncommitted:
                        readonly.execute('BEGIN')
                    error = ValueError if uncommitted else sqlite3.OperationalError
                    with pytest.raises(error, match='standalone durable commit|readonly'):
                        send_ops(readonly, [dict(client_op_id=row[0])], {})
        finally:
            hub.close()
    assert len(requests) == 1 and observations == []
    assert history(live, row[0])['exposures'] == []
