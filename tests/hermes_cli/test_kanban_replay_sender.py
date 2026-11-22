"""Real outbox tracer; controlled serving-Board replies, not live rollout proof."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_replay_journal import binding


class Receiver:
    """Control reply shapes from Board.replayInspect/replayControl; W=1 fixture."""
    def __init__(self):
        self.slot = dict(binding(), mode='STRICT', session='1', h='0', a='0', last_verdict=None)
        self.calls = []
        self.sent = []
        self.lose_reply = False
        self.supported = True
        self.status = 'applied'

    def _call(self, method, path, body=None):
        action = path.rsplit('/', 1)[-1]
        self.calls.append(action)
        if action == 'capability':
            return dict(processed_prefix=self.supported, board_incarnation=self.slot['board_incarnation'],
                        gateway_principal=self.slot['gateway_principal'])
        if action == 'inspect':
            return {'slot': dict(self.slot)}
        if action == 'session':
            assert body['expected'] == self.slot
            self.slot['session'] = str(int(self.slot['session']) + 1)
            return {'slot': dict(self.slot)}
        if action == 'ack':
            assert body['ack'] == dict(sequence=self.slot['h'], op_id=self.slot['last_op_id'],
                                       payload_sha256=self.slot['last_payload_sha256'])
            self.slot['a'] = self.slot['h']
            return {'slot': dict(self.slot)}
        assert action == 'ops' and '/replay/' in path
        assert body['journal_id'] == self.slot['journal_id']
        op, = body['ops']
        assert op['replay']['session'] == self.slot['session']
        self.sent.append(op)
        self.slot.update(h=op['replay']['sequence'], last_op_id=op['client_op_id'],
                         last_payload_sha256=op['replay']['payload_sha256'],
                         last_verdict=json.dumps(dict(client_op_id=op['client_op_id'], status=self.status, version=7)))
        if self.lose_reply:
            raise OSError('reply lost after commit')
        return {'protocol': 'processed_prefix1', 'results': []}

    def ops(self, *args):
        pytest.fail('bound replay journal fell back to legacy transport')


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    client.ensure_identity(conn, 'default', 'macbook')
    with kbc.write_txn(conn):
        journal.initialize(conn, binding(), board_id=conn.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0])
    yield conn
    conn.close()


def test_real_outbox_is_issued_sent_recovered_and_pruned(live):
    tid = kb.create_task(live, title='tracer', assignee='worker')
    receiver = Receiver()
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1
    assert receiver.sent[0]['uid'] == tid
    assert journal.snapshot(live)['settled'] == '1'
    assert journal.pending_wire(live) is None
    assert not live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?',
                            (receiver.sent[0]['client_op_id'],)).fetchone()
    assert receiver.calls == ['capability', 'inspect', 'session', 'ops', 'inspect', 'ack']
    assert receiver.slot['a'] == '1'
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 7


def test_lost_response_recovers_exact_verdict_without_resending(live):
    kb.create_task(live, title='lost response', assignee='worker')
    receiver = Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    frozen = journal.pending_wire(live)
    assert frozen is not None
    receiver.lose_reply = False
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1 and len(receiver.sent) == 1
    assert journal.pending_wire(live) is None


def test_capability_loss_retains_custody_and_never_uses_legacy(live):
    kb.create_task(live, title='cap lost', assignee='worker')
    receiver = Receiver()
    receiver.supported = False
    with pytest.raises(ValueError, match='replay capability unavailable'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] > 0
    assert journal.pending_wire(live) is None and not receiver.sent


def test_production_hub_authenticates_and_drains_over_http(live):
    receiver = Receiver()
    authorizations = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply('GET')

        def do_POST(self):
            self.reply('POST')

        def reply(self, method):
            auth = self.headers.get('Authorization')
            authorizations.append(auth)
            if auth != 'Bearer fixture-key':
                self.send_error(401)
                return
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length)) if length else None
            data = json.dumps(receiver._call(method, self.path, body)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    hub = client.Hub(client.Identity(f'http://127.0.0.1:{server.server_port}', 'fixture-key', 'macbook'))
    try:
        kb.create_task(live, title='HTTP tracer', assignee='worker')
        result = client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
        assert result.applied == 1 and receiver.slot['a'] == '1'
        assert len(authorizations) == 6 and set(authorizations) == {'Bearer fixture-key'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize('defect', ['missing', 'changed'])
def test_source_loss_or_changed_frozen_envelope_retains_journal(live, defect):
    kb.create_task(live, title='source custody', assignee='worker')
    receiver = Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    frozen = journal.pending_wire(live)
    op_id = journal.snapshot(live)['pending']['op_id']
    with kbc.write_txn(live):
        if defect == 'missing':
            live.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (op_id,))
        else:
            live.execute("UPDATE kanban_sync_outbox SET wire_json='{}' WHERE op_id=?", (op_id,))
    receiver.lose_reply = False
    with pytest.raises(ValueError, match='issued source custody'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert journal.pending_wire(live) == frozen
    assert journal.snapshot(live)['settled'] == '0'
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1


@pytest.mark.parametrize('status', ['rejected', 'cancelled', 'deferred'])
def test_unintegrated_disposition_stays_in_custody_without_ack(live, status):
    kb.create_task(live, title='final custody', assignee='worker')
    receiver = Receiver()
    receiver.status = status
    for _ in range(2):
        with pytest.raises(ValueError, match='tracer final disposition requires custody integration'):
            client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert journal.snapshot(live)['pending'] is not None
        assert journal.snapshot(live)['settled'] == '0'
        assert receiver.slot['a'] == '0'
    assert len(receiver.sent) == 1


@pytest.mark.parametrize('flag', ['primary', 'before_head'])
def test_unintegrated_push_modes_refuse_before_network(live, flag):
    kb.create_task(live, title='mode custody', assignee='worker')
    receiver = Receiver()
    options = dict(primary=False, max_batches=1)
    options[flag] = True
    with pytest.raises(ValueError, match='tracer push mode not integrated'):
        client.push(live, receiver, 'default', 'macbook', **options)
    assert not receiver.calls and journal.pending_wire(live) is None

