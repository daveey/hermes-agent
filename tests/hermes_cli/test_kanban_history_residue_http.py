"""Real HTTP custody producer: uncertain delivery is GET-only after reopen."""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli import kanban_history_residue_http as transport
from hermes_cli.kanban_history_residue_source import Refused
from hermes_cli.kanban_sync_client import Hub, Identity
from tests.hermes_cli.test_kanban_history_residue_custody import SCOPE, request


@pytest.fixture
def server():
    raw, receipt = request()
    state = {'calls': [], 'receipt': None, 'reply': json.dumps(receipt).encode(), 'post': 'ok'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(False)

        def do_POST(self):
            self.respond(True)

        def respond(self, post):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            state['calls'].append((self.command, self.path, self.headers.get('Authorization'), body))
            if post:
                state['receipt'] = state['reply']
                if state['post'] == 'drop':
                    self.close_connection = True
                    return
            response = state['receipt'] or b'null'
            if state.get('truncated'):
                self.send_response(200)
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.write(b'10\r\nxx')
                self.close_connection = True
                return
            if state.get('slow'):
                self.send_response(state.get('status', 200))
                self.send_header('Content-Length', '100')
                self.end_headers()
                try:
                    self.wfile.write(b'{')
                    self.wfile.flush()
                    state['release'].wait(2)
                finally:
                    state['peer_finished'].set()
                return
            self.send_response(state.get('status', 200))
            if 'redirect' in state:
                self.send_header('Location', state['redirect'])
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    hub = Hub(Identity(f'http://127.0.0.1:{http.server_port}', 'fixture-key', 'home'))
    try:
        yield hub, state, raw
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
        hub.close()


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / 'history.db', isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def scope(hub):
    return (hub.ident.url, 'default', 'home')


def test_real_authenticated_post_exact_body_then_get_readback(db, server):
    hub, state, raw = server
    result = transport.observe(db, hub, 'default', raw)
    assert result['recorded'] is True and result['current_authority_verified'] is False
    assert [c[0] for c in state['calls']] == ['POST', 'GET']
    assert state['calls'][0][3] == raw.encode()
    assert state['calls'][0][1] == '/v2/boards/default/history-residue'
    assert state['calls'][1][1] == '/v2/boards/default/history-residue?op_id=op-original'
    assert state['calls'][1][3] == b''
    assert all(c[2] == 'Bearer fixture-key' for c in state['calls'])
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] == state['reply'].decode()
    before = db.total_changes
    transport.observe(db, hub, 'default', raw)
    assert state['calls'][-1][0] == 'GET' and len(state['calls']) == 3
    assert db.total_changes == before


def test_definitive_post_error_surfaces_status_and_code_without_repost(db, server):
    from hermes_cli.kanban_sync_client import HubError
    hub, state, raw = server
    state.update(status=409, reply=b'{"error":"history_residue_op_already_known"}')
    with pytest.raises(HubError, match='history_residue_op_already_known') as raised:
        transport.observe(db, hub, 'default', raw)
    assert raised.value.status == 409
    assert [c[0] for c in state['calls']] == ['POST']
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None
    state['status'] = 200
    state['receipt'] = None
    with pytest.raises(Refused, match='observation_unconfirmed'):
        transport.observe(db, hub, 'default', raw)
    assert [c[0] for c in state['calls']] == ['POST', 'GET']


def test_error_body_timeout_preserves_status_and_get_only_intent(db, server):
    from hermes_cli.kanban_sync_client import HubError
    hub, state, raw = server
    hub.timeout = 0.15
    state.update(status=409, slow=True, release=threading.Event(), peer_finished=threading.Event())
    try:
        with pytest.raises(HubError) as raised:
            transport.observe(db, hub, 'default', raw)
        assert raised.value.status == 409
        assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None
    finally:
        state['release'].set()
        assert state['peer_finished'].wait(2)
    state.update(status=200, slow=False, receipt=None)
    with pytest.raises(Refused, match='observation_unconfirmed'):
        transport.observe(db, hub, 'default', raw)
    assert [c[0] for c in state['calls']] == ['POST', 'GET']


def test_dropped_post_answer_reconciles_without_repost(db, server):
    hub, state, raw = server
    state['post'] = 'drop'
    with pytest.raises(OSError):
        transport.observe(db, hub, 'default', raw)
    assert transport.observe(db, hub, 'default', raw)['recorded']
    assert [c[0] for c in state['calls']] == ['POST', 'GET']


def test_restart_unsealed_intent_only_gets_even_if_server_has_no_record(db, server):
    hub, state, raw = server
    custody.stage(db, scope(hub), raw)
    path = db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as reopened:
        with pytest.raises(Refused, match='observation_unconfirmed'):
            transport.observe(reopened, hub, 'default', raw)
    assert [c[0] for c in state['calls']] == ['GET']
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None


@pytest.mark.parametrize('change', ['source', 'reason', 'revision', 'duplicate', 'authority', 'utf8', 'large'])
def test_bad_authenticated_readback_keeps_intent_unsealed(db, server, change):
    hub, state, raw = server
    obj = json.loads(state['reply'])
    if change == 'source': obj['source_json'] += ' '
    if change == 'reason': obj['reason'] = 'another reason'
    if change == 'revision': obj['parent']['version'] += 1
    if change == 'authority': obj['execution_authority'] = True
    state['reply'] = json.dumps(obj).encode()
    if change == 'duplicate': state['reply'] = state['reply'][:-1] + b',"version":1}'
    if change == 'utf8': state['reply'] = b'\xff'
    if change == 'large': state['reply'] = b' ' * (transport.MAX_RESPONSE + 1)
    with pytest.raises((Refused, ValueError, UnicodeError)):
        transport.observe(db, hub, 'default', raw)
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None


def test_changed_retry_request_refused_without_network(db, server):
    hub, state, raw = server
    custody.stage(db, scope(hub), raw)
    with pytest.raises(Refused, match='intent_conflict'):
        transport.observe(db, hub, 'default', raw + ' ')
    assert not state['calls']


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirect_never_forwards_credentials_or_reposts(db, server, status):
    hub, state, raw = server
    state.update(status=status, redirect=hub.ident.url + '/stolen')
    with pytest.raises(Exception) as result:
        transport.observe(db, hub, 'default', raw)
    assert str(status) in str(result.value)
    assert all('/stolen' not in c[1] for c in state['calls'])
    assert [c[0] for c in state['calls']] == ['POST']
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None


def test_nonloopback_cleartext_refused_before_custody_or_io(db):
    raw, _ = request()
    hub = Hub(Identity('http://board.example', 'key', 'home'))
    with pytest.raises(Refused, match='endpoint'):
        transport.observe(db, hub, 'default', raw)
    assert not db.execute('SELECT name FROM sqlite_master').fetchall()


def test_missing_readback_cannot_turn_post_success_into_recorded(db, server, monkeypatch):
    hub, _state, raw = server
    original = transport._request

    def missing(hub, method, path, body):
        answer = original(hub, method, path, body)
        return b'null' if method == 'GET' else answer

    monkeypatch.setattr(transport, '_request', missing)
    with pytest.raises(Refused, match='observation_unconfirmed'):
        transport.observe(db, hub, 'default', raw)
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None


def test_sealed_receipt_conflict_does_not_overwrite_custody(db, server):
    hub, state, raw = server
    transport.observe(db, hub, 'default', raw)
    state['receipt'] += b' '
    with pytest.raises(Refused, match='receipt_conflict'):
        transport.observe(db, hub, 'default', raw)
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] == state['reply'].decode()
