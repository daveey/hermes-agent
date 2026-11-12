"""Absolute attachment transfer budget, including headers and body. [daveey]"""
from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.request


class Transfer:
    """Own the transport sockets so expiry cancels a trickling urllib reader.

    DNS cannot be interrupted; a connection completing after expiry is shut down
    before it can send credentials. No retries or redirects occur here.
    """

    def __init__(self, budget: float):
        self.deadline = time.monotonic() + budget
        self.lock = threading.Lock()
        self.expired = False
        self.sockets = []

    @staticmethod
    def cut(sock):
        try:
            # Bypass SSLSocket methods while its owner may be inside SSL_read.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass

    def connection(self, base):
        transfer = self

        class Connection(base):
            def register_socket(self):
                with transfer.lock:
                    if transfer.expired:
                        transfer.cut(self.sock)
                        self.close()
                        raise TimeoutError("attachment transfer deadline exceeded")
                    transfer.sockets.append(self.sock)

            def _tunnel(self):
                # CONNECT reads occur inside HTTPConnection.connect, before the
                # outer connect returns. Own the raw socket before those reads.
                self.register_socket()
                return super()._tunnel()

            def connect(self):
                if issubclass(base, http.client.HTTPSConnection):
                    http.client.HTTPConnection.connect(self)
                    self.register_socket()
                    # Register the SSL socket before its blocking handshake:
                    # wrap_socket detaches the raw descriptor we registered.
                    self.sock = self._context.wrap_socket(
                        self.sock, server_hostname=self._tunnel_host or self.host,
                        do_handshake_on_connect=False)
                    self.register_socket()
                    self.sock.do_handshake()
                else:
                    super().connect()
                self.register_socket()

        return Connection

    def handlers(self):
        http_connection = self.connection(http.client.HTTPConnection)
        https_connection = self.connection(http.client.HTTPSConnection)

        class HTTP(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(http_connection, req)

        class HTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(https_connection, req, context=self._context)

        return HTTP(), HTTPS()

    def run(self, operation):
        box = {}

        def worker():
            try:
                box["result"] = operation()
            except BaseException as exc:  # health: allow BLE001 -- re-raised on calling thread
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True, name="kanban-attachment-transfer")
        thread.start()
        thread.join(max(0.0, self.deadline - time.monotonic()))
        if thread.is_alive() or time.monotonic() >= self.deadline:
            with self.lock:
                self.expired = True
                for sock in self.sockets:
                    self.cut(sock)
            raise TimeoutError("attachment transfer deadline exceeded")
        if "error" in box:
            raise box["error"]
        return box["result"]
