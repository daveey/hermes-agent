"""Legacy never-enrolled pushes: no replay-capability status may stop the healthy pull."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support


@pytest.mark.parametrize('cap_status', [200, 404, 400, 401, 403, 500, 410])
def test_legacy_push_capability_status(tmp_path, monkeypatch, cap_status):
    monkeypatch.setattr(client.Hub, '_proxied', staticmethod(lambda parts: False))
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / 'kanban.db'
    conn = kbc.connect(path)
    client.ensure_identity(conn, 'default', 'macbook')
    kb.create_task(conn, title='legacy probe', assignee='worker')
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append(('GET', self.path))
            if self.path.endswith('/replay/capability'):
                self.reply(cap_status, {} if cap_status == 200 else {'error': 'x'})
            elif '/changes?' in self.path:
                self.reply(200, dict(changes=[], next=0, head=0, more=False))
            else:
                self.reply(200, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(('POST', self.path))
            self.reply(200, {'results': [dict(client_op_id=op['client_op_id'], status='applied', version=1)
                                         for op in body['ops']]})

        def log_message(self, *_a):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    hub = client.Hub(client.Identity(f'http://127.0.0.1:{server.server_port}', 'k', 'macbook'))
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    try:
        results = client.sync_all_once(hub, boards=[('default', path)])
        pending = conn.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
        pulled = any('/changes?' in p for _m, p in seen)
        posted = any(m == 'POST' and p.endswith('/ops') for m, p in seen)
        print(f'\nPROBE status={cap_status} error={results[0].error!r} posted={posted} pulled={pulled} pending={pending}')
        assert pulled, f'optional probe prevented healthy pull: {results[0].error!r}'
        if cap_status in (200, 404, 410):
            assert posted and pending == 0, 'legacy ops must be sent and the outbox drained'
        else:
            assert not posted and pending > 0, 'a refusing probe must retain custody of the pending row'
    finally:
        hub.close(); server.shutdown(); t.join(5); server.server_close(); conn.close()
