"""Authorized process-local lifecycle facade for fact replicas. [daveey]

Startup supplies already-authorized replica handles. This facade is not a
profile authorization resolver or a distributed release coordinator. It never
activates from config or readiness alone. No request discovery or acquisition.
"""
from __future__ import annotations

import threading
from contextvars import copy_context
from functools import partial

from hermes_cli.fact_contract import FactKey, Position, Ready, Pending, Unavailable

from hermes_cli.fact_store import view_key


class ScopedV1:
    key = staticmethod(FactKey)
    position = staticmethod(Position)
    view_key = staticmethod(view_key)
    Ready, Pending, Unavailable = Ready, Pending, Unavailable

    def __init__(self, handle):
        self._handle = handle

    def _scope(self, scope):
        if scope != self._handle.scope:
            raise ValueError('unauthorized_scope')
        if self._handle._closing or self._handle._closed:
            raise RuntimeError('fact handle closing or closed')

    def snapshot(self, scope):
        self._scope(scope)
        return self._handle._replica.v1.snapshot(scope)

    def subscribe(self, scope, kinds, keys, after, callback):
        self._scope(scope)
        bound_callback = partial(copy_context().run, callback)
        return self._handle._replica.v1.subscribe(scope, kinds, keys, after, bound_callback)

    def changes(self, scope, after, *, kinds=(), keys=None):
        self._scope(scope)
        return self._handle._replica.v1.changes(scope, after, kinds=kinds, keys=keys)

    def bind_consumer(self, name, scope, on_mode):
        with self._handle._lock:
            self._scope(scope)
            return self._handle._lifecycle.bind_consumer(name, scope, on_mode)

    def report_ready(self, name, scope, *, version=1, ready=True):
        self._scope(scope)
        return self._handle._lifecycle.report_ready(name, scope, version=version, ready=ready)


class ScopedHandle:
    """Startup-issued facade; do not construct from request profile/path strings."""
    def __init__(self, replica, lifecycle):
        self.scope = replica.scope
        self._replica, self._lifecycle = replica, lifecycle
        self._closed = False
        self._closing = False
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self.v1 = ScopedV1(self)

    def demand(self, view, selector):
        self.v1._scope(self.scope)
        self._replica.demand(view, selector)

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._lifecycle.has_bindings(self.scope):
                raise RuntimeError('fact handle retains consumer bindings')
            self._closing = True
        # Never hold the handle lock while joining replica callback threads.
        # Failed close retains closing state and can be explicitly retried.
        with self._close_lock:
            if not self._closed:
                self._replica.close()
                self._closed = True
