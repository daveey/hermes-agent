"""Fail-closed bootstrap HTTP listener; transfer the same socket to the full ASGI app.

No plugins, configuration, credentials, or application state are read here. Health
is public in the full app too. All other requests fail closed until its complete
authentication stack has been assembled. Application assembly runs concurrently
with this small responder, not on its serving thread.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import socket
import sys
import threading


class _BootstrapServer(HTTPServer):
    request_queue_size = 2048  # match uvicorn during reconnect bursts

    def server_bind(self):
        if sys.platform == 'win32':
            # SO_REUSEADDR on Windows can bind over another live listener.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        pass  # disconnected clients have no application side effects


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        self.request.settimeout(0.1)
        super().setup()
        # A socket timeout alone is renewed by every dribbled byte. Bound the
        # entire connection, including headers/writes, so even a pre-auth client
        # cannot hold the single responder or the eventual shutdown/handoff.
        self.deadline = threading.Timer(0.25, self.expire)
        self.deadline.daemon = True
        self.deadline.start()

    def expire(self):
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        try:
            super().finish()
        finally:
            self.deadline.cancel()

    def do_GET(self):
        if self.command in ('GET', 'HEAD') and self.path.split('?', 1)[0] == '/api/health':
            status, body = 503, b'{"status":"starting","ready":false,"ok":false}'
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
        self.servers = []
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            seen = set()
            for family, _, _, _, address in addresses:
                if (family, address) in seen:
                    continue
                seen.add((family, address))

                class Server(_BootstrapServer):
                    address_family = family

                # Hostnames may resolve to both loopbacks. Retain every socket,
                # like uvicorn, instead of silently narrowing localhost to IPv4.
                bound = (address[0], port, *address[2:])
                server = Server(bound, _Handler)
                self.servers.append(server)
                if not port:
                    port = server.server_address[1]
        except OSError as exc:
            for server in self.servers:
                server.server_close()
            from hermes_cli.web_server_lifecycle import (
                _is_addr_in_use_error, _report_port_in_use, PORT_IN_USE_EXIT_CODE,
            )
            if not _is_addr_in_use_error(exc):
                raise
            _report_port_in_use(host, port)
            raise SystemExit(PORT_IN_USE_EXIT_CODE) from None
        self.server = self.servers[0]
        self.port = port
        self.threads = [threading.Thread(target=server.serve_forever,
                        kwargs={'poll_interval': 0.01}, name='serve-bootstrap-http', daemon=True)
                        for server in self.servers]
        self.thread = self.threads[0]
        for thread in self.threads:
            thread.start()
        self.stopped = False

    def handoff(self):
        if not self.stopped:
            for server, thread in zip(self.servers, self.threads):
                server.shutdown()
                thread.join()
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
            await server.startup(sockets=[item.socket for item in self.servers])
        finally:
            server.lifespan = original

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handoff()
        for server in self.servers:
            server.server_close()
