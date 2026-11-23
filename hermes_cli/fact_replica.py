"""Scoped in-memory replicas shared by serve and gateway lifecycle bindings. [daveey]"""
from __future__ import annotations

from dataclasses import dataclass, replace

import socket
import threading
import logging
import time

from hermes_cli.fact_contract import Delta, Fact, FactKey, Position, Ready, Pending, Unavailable
from hermes_cli.fact_ipc import connect, receive, send
from hermes_cli.fact_demand import DemandChannel
from hermes_cli.fact_map import FactMap
from hermes_cli.fact_pages import MAX_RESET_BYTES
from hermes_cli.fact_store import FactStore, Snapshot, view_key
from hermes_cli.fact_wire import decode, encode, encoded_size


@dataclass(frozen=True)
class Endpoint:
    path: str
    token: str


@dataclass(frozen=True)
class _Snapshot(Snapshot):
    online: bool = False

    def get(self, key):
        fact = super().get(key)
        return fact if self.online or fact is None else replace(fact, health="stale", error_code="owner_disconnected")


class ReplicaV1:
    key = staticmethod(FactKey)
    position = staticmethod(Position)
    view_key = staticmethod(view_key)
    Ready, Pending, Unavailable = Ready, Pending, Unavailable

    def __init__(self, handle):
        self._handle = handle

    def _scope(self, scope):
        if scope != self._handle.scope:
            raise ValueError("unauthorized_scope")

    def snapshot(self, scope):
        self._scope(scope)
        h = self._handle
        with h._store._condition:
            snap = h._store.snapshot(scope)
            return _Snapshot(scope, snap.epoch, snap.rev, snap._facts, h._online)

    def subscribe(self, scope, kinds, keys, after, callback):
        self._scope(scope)
        def deliver(delta):
            callback(self._health(delta))
        sub = self._handle._store.subscribe(scope, kinds, keys, after, deliver)
        self._handle._deliver.set()
        return sub

    def changes(self, scope, after, *, kinds=(), keys=None):
        self._scope(scope)
        return self._health(self._handle._store.changes(scope, after, kinds=kinds, keys=keys))

    def _health(self, delta):
        if self._handle._online:
            return delta
        return replace(delta, upserts=tuple(replace(f, health="stale", error_code="owner_disconnected")
                                            for f in delta.upserts))


class ReplicaHandle:
    def __init__(self, scope, endpoint):
        self.scope, self.endpoint = scope, endpoint
        self._store = FactStore()
        self._online = False
        self._stop = threading.Event()
        self._deliver = threading.Event()
        self._demands = DemandChannel(scope, endpoint)
        self._socket = None
        self.v1 = ReplicaV1(self)
        self._reader = threading.Thread(target=self._run, name="fact-replica", daemon=True)
        self._callbacks = threading.Thread(target=self._dispatch, name="fact-callbacks", daemon=True)
        self._reader.start()
        self._callbacks.start()

    def demand(self, view, selector):
        # Startup/prewarm callers enqueue only; never socket or DB I/O on a request.
        self._demands.put(view, selector)

    def _dispatch(self):
        retries = {}
        timeout = None
        while not self._stop.is_set():
            self._deliver.wait(timeout)
            self._deliver.clear()
            if self._stop.is_set():
                break
            with self._store._condition:
                subscriptions = set(self._store._subscribers)
                pending = [s for s in subscriptions if s.queue or s.reset_pending]
            retries = {s: retry for s, retry in retries.items() if s in pending}
            for sub in pending:
                due, count = retries.get(sub, (0, 0))
                if due > time.monotonic():
                    continue
                if not sub.delivery_lock.acquire(blocking=False):
                    retries[sub] = (time.monotonic() + .1, count)
                    continue
                try:
                    sub._deliver()
                    retries.pop(sub, None)
                except Exception:  # health: allow BLE001 -- isolate subscribers and retry retained reset
                    delay = (.1, .2, .4, 1., 2., 5.)[min(count, 5)]
                    retries[sub] = (time.monotonic() + delay, min(count + 1, 5))
                    logging.getLogger(__name__).exception('replica callback failed; reset retained')
                finally:
                    sub.delivery_lock.release()
            # Publication wakes healthy peers immediately, but cannot bypass a
            # failing subscriber's backoff. Quiet owners still get a retry.
            timeout = (max(0, min(due for due, _ in retries.values()) - time.monotonic())
                       if retries else None)

    def _apply(self, delta):
        if not isinstance(delta, Delta) or type(delta.rev) is not int or type(delta.prev) is not int:
            raise ValueError("invalid_delta")
        if not all(isinstance(f, Fact) for f in delta.upserts):
            raise ValueError("invalid_fact")
        if any(f.key.scope != self.scope for f in delta.upserts) or any(k.scope != self.scope for k in delta.deletes):
            raise ValueError("foreign_scope_in_transport")
        store = self._store
        # Build the immutable generation outside the request-reader lock.
        sizes = FactMap() if delta.reset else store._sizes
        facts = FactMap() if delta.reset else store._facts
        old_keys = set(store._facts) if delta.reset else set()
        for key in delta.deletes:
            facts = facts.discard(key)
            sizes = sizes.discard(key)
        for fact in delta.upserts:
            facts = facts.put(fact.key, fact)
            sizes = sizes.put(fact.key, encoded_size(fact) + 32)
        changed = frozenset(old_keys | {f.key for f in delta.upserts} | set(delta.deletes))
        size = 1024 + sum(sizes.get(key, len(encode(key))) for key in changed)
        with store._condition:
            if not delta.reset and (delta.epoch != store._epoch or delta.prev != store._rev):
                raise ValueError("replica_gap")
            store._facts, store._epoch, store._rev = facts, delta.epoch, delta.rev
            store._sizes = sizes
            # Reader ring is bounded too; owner positions remain authoritative.

            if delta.reset:
                store._ring.clear()
                store._retained_bytes = 0
            if delta.reset or not self._online:
                self._reset_subscribers()
            store._ring.append((delta.rev, changed, size))
            store._retained_bytes += size
            while len(store._ring) > store._ring_revisions or store._retained_bytes > store._ring_bytes:
                store._retained_bytes -= store._ring.popleft()[2]
            self._online = True
            for sub in store._subscribers:
                sub.enqueue()
            store._condition.notify_all()
        self._deliver.set()

    def _reset_subscribers(self):
        for sub in self._store._subscribers:
            sub.queue.clear()
            sub.queued_bytes = 0
            sub.reset_pending = True

    def _reset(self, sock, pin):
        size = pin["size"]
        if type(size) is not int or not 0 < size <= MAX_RESET_BYTES:
            raise ValueError("reset_budget_exceeded")
        data = bytearray()
        while len(data) < size:
            send(sock, {"op": "page", "token": pin["token"], "offset": len(data)})
            chunk = receive(sock)
            if not isinstance(chunk, bytes) or not chunk or len(data) + len(chunk) > size:
                raise ValueError("invalid_reset_chunk")
            data.extend(chunk)
        delta = decode(data)
        del data
        if not delta.reset or delta.epoch != pin["epoch"] or delta.rev != pin["rev"]:
            raise ValueError("reset_identity_mismatch")
        self._apply(delta)
        send(sock, {"op": "release", "token": pin["token"]})
        receive(sock)

    def _session(self, sock):
        snap = self._store.snapshot(self.scope)
        send(sock, {"version": 1, "token": self.endpoint.token, "after": Position(snap.epoch, snap.rev)})
        if receive(sock)["scope"] != self.scope:
            raise ValueError("unauthorized_scope")
        while not self._stop.is_set():

            send(sock, {"op": "next"})
            result = receive(sock)
            if "pin" in result:
                self._reset(sock, result["pin"])
                self._demands.owner_epoch(result["pin"]["epoch"])
            elif "delta" in result:
                self._apply(result["delta"])
                self._demands.owner_epoch(result["delta"].epoch)
            elif result.get("idle"):
                with self._store._condition:
                    if not self._online:
                        self._reset_subscribers()
                        self._deliver.set()
                    self._online = True


    def _run(self):
        retry = .05
        while not self._stop.is_set():
            try:
                with connect(self.endpoint.path) as sock:
                    self._socket = sock
                    self._session(sock)
            except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
                with self._store._condition:
                    was_online = self._online
                    self._online = False
                    if was_online:
                        for sub in self._store._subscribers:
                            sub.queue.clear()
                            sub.queued_bytes = 0
                            sub.reset_pending = True
                if was_online:
                    self._deliver.set()
                logging.getLogger(__name__).debug("Fact replica disconnected: %s", type(exc).__name__)
                if self._stop.wait(retry):
                    break
                retry = min(retry * 2, 2)
            finally:
                self._socket = None

    def close(self):
        self._stop.set()
        self._demands.close()
        self._deliver.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # peer may have closed between capture and shutdown
        self._reader.join(5)
        self._callbacks.join(5)
        if self._reader.is_alive() or self._callbacks.is_alive():
            raise RuntimeError("replica_shutdown_timeout")
