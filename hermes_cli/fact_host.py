"""Single-process fact owner and startup-authorized scoped IPC. [daveey]

The coordinator supplies bootstrap and OS process supervision. Readers never
spawn owners, discover profiles, or acquire DB handles on request paths.
"""
from __future__ import annotations


import hmac
import os
from pathlib import Path
import socketserver
import threading

from hermes_cli.fact_contract import Position
from hermes_cli.fact_expiry import ExpiryWorker
from hermes_cli.fact_ipc import receive, send
from hermes_cli.fact_pages import PinnedPages
from hermes_cli.fact_store import FactStore, reset_delta



class _Server(getattr(socketserver, "ThreadingUnixStreamServer", socketserver.ThreadingTCPServer)):
    daemon_threads = True
    block_on_close = False

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        subscription = None
        pins = set()
        owner = self.server.owner
        scope = None
        try:
            hello = receive(self.request)
            if hello.get("version") != 1:
                raise ValueError("unsupported_contract")
            scope = owner.authorize(hello["token"])
            subscription = owner.store.subscribe(scope, (), None, hello.get("after"), lambda _: None)
            self.request.settimeout(60)
            send(self.request, {"scope": scope, "epoch": owner.store.snapshot(scope).epoch})
            while not owner.closed.is_set():
                command = receive(self.request)
                if owner.closed.is_set():
                    return
                handlers = {"next": self._next, "page": self._page,
                            "release": self._release, "demand": self._demand}
                handler = handlers.get(command["op"])
                if handler is None:
                    raise ValueError("unknown_operation")
                send(self.request, handler(owner, scope, subscription, pins, command))
        except (EOFError, OSError, ValueError, KeyError, IndexError, TypeError, RecursionError):
            # Disconnect means no ACK. Replica reconnects at its last applied cursor.
            return
        finally:
            if subscription is not None:
                subscription.close()
            for token in pins:
                owner.pages.release(token, scope)


    @staticmethod
    def _next(owner, scope, subscription, pins, command):
        for token in pins:
            owner.pages.release(token, scope)
        pins.clear()
        response = owner.next(subscription)
        if "pin" in response:
            pins.add(response["pin"]["token"])
        return response

    @staticmethod
    def _page(owner, scope, subscription, pins, command):
        if command["token"] not in pins:
            raise ValueError("foreign_reset")
        return owner.pages.page(command["token"], scope, command["offset"])

    @staticmethod
    def _release(owner, scope, subscription, pins, command):
        if command["token"] not in pins:
            raise ValueError("foreign_reset")
        owner.pages.release(command["token"], scope)
        pins.discard(command["token"])
        return True

    @staticmethod
    def _demand(owner, scope, subscription, pins, command):
        try:
            owner.demand(scope, command["view"], command["selector"])
        except (ValueError, KeyError, TypeError):
            return {"accepted": False}
        return {"accepted": True}


class FactHost:
    def __init__(self, directory, capabilities, *, store=None, demand=None, page_ttl=30.0):
        self.directory = Path(directory)
        self.capabilities = dict(capabilities)
        if not self.capabilities or any(not isinstance(t, str) or len(t) < 32 for t in self.capabilities):
            raise ValueError("startup-issued capability tokens required")
        self.store = store if store is not None else FactStore()
        self.demand = demand if demand is not None else self._no_demand
        self.pages = PinnedPages(ttl=page_ttl, directory=self.directory)
        self.closed = threading.Event()
        self._lock_file = None
        self._server = None
        self._thread = None
        self._expiry = None

    @staticmethod
    def _no_demand(scope, view, selector):
        raise ValueError("materializer_not_registered")

    def authorize(self, token):
        if not isinstance(token, str):
            raise ValueError("unauthorized")
        for issued, scope in self.capabilities.items():
            if hmac.compare_digest(issued, token):
                return scope
        raise ValueError("unauthorized")

    def start(self):
        getuid = getattr(os, "getuid", None)
        if os.name != "posix" or getuid is None:
            raise NotImplementedError("fact host requires POSIX Unix sockets and flock")
        import fcntl

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat = self.directory.stat()
        if stat.st_uid != getuid() or stat.st_mode & 0o077:
            raise ValueError("fact host directory must be private to its owner")
        self._lock_file = (self.directory / "owner.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise
        try:
            path = self.directory / "facts.sock"
            path.unlink(missing_ok=True)  # exclusive kernel lock fences a dead predecessor
            self._server = _Server(str(path), _Handler)
            path.chmod(0o600)
            self._server.owner = self
            self._server.slots = threading.BoundedSemaphore(8)
            self._thread = threading.Thread(target=self._server.serve_forever, name="fact-host-ipc", daemon=True)
            self._thread.start()
            self._expiry = ExpiryWorker(self.store)
        except (OSError, RuntimeError):
            self.close()
            raise
        return self

    def next(self, subscription):
        snapshot = None
        with self.store._condition:
            self.store._condition.wait_for(
                lambda: subscription.queue or subscription.reset_pending or self.closed.is_set(), timeout=20)
            if self.closed.is_set():
                raise EOFError("owner closed")
            if subscription.reset_pending:
                snapshot = self.store.snapshot(subscription.scope)
                subscription.reset_pending = False
                subscription.position = Position(snapshot.epoch, snapshot.rev)
            elif subscription.queue:
                delta, size = subscription.queue.popleft()
                subscription.queued_bytes -= size
                if size > 512 * 1024:
                    snapshot = self.store.snapshot(subscription.scope)
                    subscription.queue.clear()
                    subscription.queued_bytes = 0
                    subscription.position = Position(snapshot.epoch, snapshot.rev)
            else:
                return {"idle": True}
        # Encoding / pinning / slow socket writes never hold the publication lock.
        if snapshot is not None:
            delta = reset_delta(snapshot, subscription.kinds, subscription.keys)
            return {"pin": self.pages.pin(subscription.scope, delta)}
        return {"delta": delta}

    def close(self):
        self.closed.set()
        if self._expiry is not None:
            self._expiry.close()
        with self.store._condition:
            self.store._condition.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)
        self.pages.close()
        if self._lock_file is not None:
            (self.directory / "facts.sock").unlink(missing_ok=True)
            self._lock_file.close()
            self._lock_file = None


def acquire(authorized_scope, *, endpoint):
    """Startup only: endpoint is supplied by the authorized in-memory registry."""
    from hermes_cli.fact_replica import ReplicaHandle
    return ReplicaHandle(authorized_scope, endpoint)
