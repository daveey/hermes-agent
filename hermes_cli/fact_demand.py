"""Bounded startup demand custody independent of the long-poll stream. [daveey]"""
from dataclasses import dataclass
import queue
import threading
from time import monotonic

from hermes_cli.fact_ipc import connect, receive, send
from hermes_cli.fact_store import view_key
from hermes_cli.fact_wire import decode, encode


@dataclass
class _Demand:
    command: dict
    expires: float
    epoch: str | None = None
    pending: bool = True


class DemandChannel:
    def __init__(self, scope, endpoint):
        self.scope, self.endpoint = scope, endpoint
        # One bound covers queued, in-flight, and accepted requests (<= 4 MiB).
        self._active = {}
        self._lock = threading.Lock()
        self._epoch = None
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name="fact-demand", daemon=True)
        self.thread.start()

    def _expire(self):
        now = monotonic()
        for key, entry in list(self._active.items()):
            if entry.expires <= now:
                del self._active[key]

    def put(self, view, selector):
        wire = encode({"op": "demand", "view": view, "selector": dict(selector)})
        if len(wire) > 64 * 1024:
            raise ValueError("demand_budget_exceeded")
        command = decode(wire)  # Retain owned data, not the caller's nested selector.
        key = view_key(self.scope, view, command["selector"])
        with self._lock:
            if self.stopped.is_set():
                raise RuntimeError("demand_closed")
            self._expire()
            if key in self._active:
                self._active[key].expires = monotonic() + 30
            else:
                if len(self._active) >= 64:
                    raise queue.Full
                self._active[key] = _Demand(command, monotonic() + 30)
        self.wake.set()

    def owner_epoch(self, epoch):
        with self._lock:
            self._expire()
            if epoch == self._epoch or self.stopped.is_set():
                return
            self._epoch = epoch
            for entry in self._active.values():
                entry.pending = entry.epoch != epoch
        self.wake.set()

    def run(self):
        while not self.stopped.is_set():
            with self._lock:
                self._expire()
                entry = next((e for e in self._active.values() if e.pending), None)
                observed = self._epoch
                timeout = min((e.expires - monotonic() for e in self._active.values()), default=None)
                self.wake.clear()
            if entry is None:
                self.wake.wait(None if timeout is None else max(0, timeout))
                continue
            try:
                epoch, accepted = self.deliver(entry.command)
            except (OSError, EOFError, ValueError, TypeError, KeyError):
                self.stopped.wait(.1)
                continue
            with self._lock:
                if accepted:
                    entry.epoch = epoch
                    # A new reset can arrive while the old owner's ACK is in flight.
                    entry.pending = self._epoch != observed and self._epoch != epoch
                else:
                    key = view_key(self.scope, entry.command["view"], entry.command["selector"])
                    if self._active.get(key) is entry:
                        del self._active[key]

    def deliver(self, command):
        with connect(self.endpoint.path) as sock:
            sock.settimeout(1)
            send(sock, {"version": 1, "token": self.endpoint.token, "channel": "demand"})
            hello = receive(sock)
            if hello["scope"] != self.scope:
                raise ValueError("unauthorized_scope")
            # Authenticate the ACK's owner on this connection without exporting
            # any facts. Older owners lacking epoch fail closed and retry.
            epoch = hello["epoch"]
            if not isinstance(epoch, str) or not epoch:
                raise ValueError("invalid_owner_epoch")
            send(sock, command)
            return epoch, receive(sock)["accepted"] is True

    def close(self):
        with self._lock:
            self.stopped.set()
            self._active.clear()
        self.wake.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("demand_shutdown_timeout")
