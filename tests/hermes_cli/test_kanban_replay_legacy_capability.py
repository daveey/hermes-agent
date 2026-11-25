"""Malformed optional capability on never-enrolled boards stays board-local.

Legacy send() probes /replay/capability before ops. A 200 with a non-object
must become an unidentified observation, never AttributeError, and must not
abort a later healthy board. {} remains the valid unidentified control.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_replay_raw_replies as raw_helpers

portable_depth_decoder = raw_helpers.portable_depth_decoder


@pytest.mark.parametrize('proxied', [False, True])
@pytest.mark.parametrize('capability', [
    None, [], 'unsupported', {}, b'{', b'\xff',
    pytest.param(b'[' * 100000 + b'0' + b']' * 100000, id='depth-limit'),
    pytest.param(b'9' * 100000, id='integer-limit'),
])
def test_legacy_malformed_capability_does_not_abort_later_board(tmp_path, monkeypatch, capability, proxied):
    monkeypatch.setattr(client.Hub, '_proxied', staticmethod(lambda parts: proxied))
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    broken_path = home / 'kanban.db'
    conn = kbc.connect(broken_path)
    client.ensure_identity(conn, 'default', 'macbook')
    assert conn.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] is None
    kb.create_task(conn, title='legacy capability isolation', assignee='worker')
    pending = conn.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
    assert pending > 0
    custody = [dict(row) for row in conn.execute('SELECT * FROM kanban_sync_outbox')]

    healthy_path = Path(tmp_path) / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append(('GET', self.path))
            if self.path.endswith('/replay/capability'):
                self.reply(capability)
            elif '/changes?' in self.path:
                self.reply(dict(changes=[], next=0, head=0, more=False))
            else:
                self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(('POST', self.path))
            self.reply({'results': [
                dict(client_op_id=op['client_op_id'], status='applied', version=1)
                for op in body['ops']]})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    hub = client.Hub(client.Identity(f'http://127.0.0.1:{server.server_port}', 'fixture-key', 'macbook'))
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    try:
        results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
        assert [r.board for r in results] == ['default', 'healthy']
        assert results[1].error is None
        assert any(method == 'GET' and path.endswith('/default/replay/capability') for method, path in seen)
        assert any('/default/changes?' in path for _method, path in seen)
        assert any('/healthy/' in path for _method, path in seen)
        if isinstance(capability, bytes):
            assert results[0].error
            assert 'HubError' in results[0].error and 'invalid JSON response' in results[0].error
            assert not any(method == 'POST' for method, _path in seen)
            retained = [dict(row) for row in conn.execute('SELECT * FROM kanban_sync_outbox')]
            assert len(retained) == len(custody)
            for before, after in zip(custody, retained):
                # Legacy freezing and failure diagnostics legitimately advance;
                # source identity, payload and pending disposition do not.
                for key in before.keys() - {'wire_json', 'attempts', 'last_error'}:
                    assert after[key] == before[key], key
                assert after['attempts'] > before['attempts']
                assert 'invalid JSON response' in after['last_error']

        else:
            assert results[0].error is None
            assert any(method == 'POST' and path.endswith('/default/ops') for method, path in seen)
            assert conn.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == 0
    finally:
        hub.close()
        server.shutdown()
        worker.join(5)
        server.server_close()
        conn.close()
