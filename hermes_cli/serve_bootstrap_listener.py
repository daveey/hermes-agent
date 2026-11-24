"""Fail-closed bootstrap HTTP listener; transfer the same socket to the full ASGI app.

No plugins, configuration, credentials, or application state are read here. Health
is public in the full app too. All other requests fail closed until its complete
authentication stack has been assembled. Application assembly runs concurrently
with this small responder, not on its serving thread.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import errno
import socket
import threading


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        self.request.settimeout(0.1)
        super().setup()

    def do_GET(self):
        if self.command in ('GET', 'HEAD') and self.path.split('?', 1)[0] == '/api/health':
            status, body = 200, b'{"status":"starting","ready":false,"ok":false}'
        else:
            status, body = 503, b'{"detail":"Application and authentication warming"}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        if status == 503:
            self.send_header('Retry-After', '1')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    # Never parse or execute request bodies during assembly.
    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_GET

    def log_message(self, format, *args):
        pass


class BootstrapListener:
    def __init__(self, host, port):
        class Server(HTTPServer):
            address_family = socket.AF_INET6 if ':' in host else socket.AF_INET

            def handle_error(self, request, client_address):
                pass  # disconnected bootstrap clients have no application side effects

        try:
            self.server = Server((host, port), _Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            from hermes_cli.web_server_lifecycle import _report_port_in_use, PORT_IN_USE_EXIT_CODE
            _report_port_in_use(host, port)
            raise SystemExit(PORT_IN_USE_EXIT_CODE) from None
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, name='serve-bootstrap-http', daemon=True)
        self.thread.start()
        self.stopped = False

    def handoff(self):
        if not self.stopped:
            self.server.shutdown()
            self.thread.join()
            self.stopped = True
        return self.server.socket

    async def startup(self, server):
        listener, original = self, server.lifespan

        class HandoffLifespan:
            async def startup(self):
                await original.startup()
                if not original.should_exit:
                    await asyncio.to_thread(listener.handoff)

            def __getattr__(self, name):
                return getattr(original, name)

        # Uvicorn starts lifespan before accepting. Keep the bootstrap responder
        # active throughout that wait; stop it only at the actual accept handoff.
        server.lifespan = HandoffLifespan()
        try:
            await server.startup(sockets=[self.server.socket])
        finally:
            server.lifespan = original

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handoff()
        self.server.server_close()
