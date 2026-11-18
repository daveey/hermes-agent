"""Inventory wire failures remain retryable without freezing an outbound."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.hermes_cli import test_origin_forward_adapter as adapter

world = adapter.world
envelope = adapter.envelope


@pytest.mark.parametrize("broken", [b'{broken-json', b'\xff', b'[]', b'null'])
def test_inventory_wire_failure_recovers(world, envelope, monkeypatch, capsys, broken):
    from hermes_cli import kanban_sync_client as client

    payload = [broken]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            raw = payload[0]
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hub = client.Hub(client.Identity(url=f'http://127.0.0.1:{server.server_port}', gateway='origin', key='fixture'))
    monkeypatch.setattr(client.Hub, 'machines', lambda self: hub._call('GET', '/v2/machines'))
    try:
        result = adapter.invoke(world, monkeypatch, capsys, envelope)
        assert result == {'status': 'pending', 'code': 'origin_forward_inventory_unavailable',
                          'handoff_complete': False}
        payload[0] = json.dumps({'me': 'origin', 'machines': [
            {'name': 'origin', 'state': 'active'}, {'name': 'owner', 'state': 'active'}]}).encode()
        first = adapter.invoke(world, monkeypatch, capsys, envelope)
        assert first['status'] == 'prepared'
        assert requests == ['/v2/machines', '/v2/machines']
        payload[0] = broken
        assert adapter.invoke(world, monkeypatch, capsys, envelope) == first
        assert len(requests) == 2  # Durable replay does not touch inventory.
    finally:
        hub.close()
        server.shutdown()
        server.server_close()
        thread.join()
