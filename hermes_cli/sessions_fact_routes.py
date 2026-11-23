"""Sessions HTTP reads from the authorized, host-owned fact replica.

The host injects the handle at startup. Neither acquisition, configuration reads,
nor projection construction belongs on the request path. Legacy serializers and
coalescers remain installed for coordinated rollback. [daveey]
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from fastapi.params import Param
from starlette.responses import Response


_UNBOUND = object()
_current = ContextVar("sessions_fact_binding", default=_UNBOUND)


@contextmanager
def use(binding):
    """Middleware pins an authorized binding; None explicitly selects legacy.

    Never derive this object from query strings or acquire it on a request.
    Context propagation keeps concurrent profiles and nested calls isolated.
    """
    token = _current.set(binding)
    try:
        yield
    finally:
        _current.reset(token)


def selector_for(signature, args, kwargs):
    """Retain every query argument, including FastAPI defaults, without I/O."""
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    return {key: value.default if isinstance(value, Param) else value
            for key, value in bound.arguments.items()}


def validate_list(selector):
    for key, choices in (("archived", ("exclude", "only", "include")),
                         ("automated", ("include", "exclude")),
                         ("order", ("created", "recent"))):
        if selector[key] not in choices:
            raise HTTPException(400, f"{key} must be one of: {', '.join(choices)}")


class SessionsFactRoutes:
    """One installed route binding, switched only by the shared release latch.

    There is no sessions polling thread to restart: the legacy reader is the
    existing request coalescer. A transition first closes its admission, drains
    admitted requests off-loop, then exposes the new mode. No second cache or
    producer is created here.
    """

    def __init__(self, *, drain_timeout=5.0):
        self._drain_timeout = drain_timeout
        self._condition = threading.Condition()
        self._transition = threading.Lock()
        self._mode = "legacy"
        self._active = 0
        self._handle = None
        self._scope = None
        self._registration = None
        self._generation = -1
        self._ready_type = None
        self._pending_type = None
        self._closed = False

    def install(self, handle, scope):
        """Startup-only injection; caller retains ownership of the host handle."""
        from hermes_cli.fact_contract import Pending, Ready

        with self._condition:
            if self._handle is not None:
                raise RuntimeError("sessions facts already installed")
            self._closed = False
            self._handle, self._scope = handle, scope
            self._ready_type, self._pending_type = Ready, Pending
        self._registration = handle.v1.bind_consumer("sessions", scope, self.on_mode)
        return self

    def on_mode(self, mode, generation):
        """Called by the shared latch OFF request threads, never from a request."""
        if mode not in ("legacy", "facts"):
            raise ValueError("unsupported sessions fact mode")
        with self._transition, self._condition:
            if generation < self._generation:
                return
            if mode == self._mode:
                self._generation = generation
                return
            if mode == "facts" and self._handle is None:
                raise RuntimeError("sessions facts not installed")
            if self._active:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass  # The host's off-loop lifecycle executor may drain.
                else:
                    raise RuntimeError("sessions transition must drain off the event loop")
            previous = self._mode
            self._mode = "transition"
            if not self._condition.wait_for(lambda: self._active == 0, timeout=self._drain_timeout):
                self._mode = previous
                raise TimeoutError("sessions legacy readers did not drain; activation refused")
            self._generation = generation
            self._mode = mode

    def close(self):
        """Withdraw the binding only after the global owner has rolled back."""
        with self._condition:
            if self._mode != "legacy" or self._active:
                raise RuntimeError("sessions binding must be idle and legacy before close")
            registration = self._registration
            self._registration = None
            self._closed = True
            self._handle = self._scope = None
            self._generation = -1
            self._ready_type = self._pending_type = None
        if registration is not None:
            registration.close()

    def _begin(self):
        with self._condition:
            mode = self._mode
            if self._closed:
                raise HTTPException(503, "sessions binding closed", headers={"Retry-After": "1"})
            if mode == "transition":
                raise HTTPException(503, "sessions mode transition", headers={"Retry-After": "1"})
            self._active += 1
            return mode

    def _end(self, task=None):
        if task is not None and not task.cancelled():
            task.exception()  # Observe detached failures after HTTP disconnect.
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def response(self, name, selector):
        result = self._handle.v1.snapshot(self._scope).view(name, selector)
        if isinstance(result, self._ready_type):
            return Response(content=result.encoded, media_type="application/json")
        if isinstance(result, self._pending_type):
            self._handle.demand(name, selector)
        raise HTTPException(503, "sessions projection unavailable", headers={"Retry-After": "1"})

    def wrap(self, name, legacy, *, signature=None, provider=None):
        """Wrap BEFORE legacy admission/cache lookup; preserve the HTTP schema."""
        signature = signature or inspect.signature(legacy)

        @functools.wraps(legacy)
        async def endpoint(*args, **kwargs):
            selector = selector_for(signature, args, kwargs)
            if name == "sessions":
                validate_list(selector)
            binding, mode = provider() if provider is not None else (self, self._begin())
            if mode == "facts":
                try:
                    return binding.response(name, selector)
                finally:
                    binding._end()
            # Shield the entire legacy path, not just its inner coalescer: a
            # disconnected request must not make its still-running scan vanish
            # from the transition's drain count.
            task = asyncio.create_task(legacy(*args, **kwargs))
            task.add_done_callback(binding._end)
            return await asyncio.shield(task)

        endpoint.__signature__ = signature
        return endpoint


class ScopedSessionsRoutes:
    """Route definitions shared across profiles, never a shared fact binding.

    With no installed consumers startup defaults to legacy. Once participating,
    middleware must select use(binding), or explicitly use(None) for a legacy
    nonparticipant. A missing context is unavailable, never an implicit scan.
    """

    def __init__(self):
        self._bindings = set()
        self._legacy = SessionsFactRoutes()
        self._participating = False

    def install(self, handle, scope):
        # Startup must precede traffic: do not orphan an unbound admitted scan.
        with self._legacy._condition:
            if self._legacy._active:
                raise RuntimeError("install sessions bindings before serving requests")
            self._bindings = {item for item in self._bindings if not item._closed}
            binding = SessionsFactRoutes().install(handle, scope)
            self._bindings.add(binding)
            self._participating = True
            return binding

    def _begin(self):
        with self._legacy._condition:
            binding = _current.get()
            if binding is None:
                return self._legacy, self._legacy._begin()  # Explicit legacy scope.
            if binding is _UNBOUND:
                if not self._participating:
                    return self._legacy, self._legacy._begin()
            elif isinstance(binding, SessionsFactRoutes) and binding in self._bindings:
                return binding, binding._begin()
            raise HTTPException(503, "sessions authorized binding missing", headers={"Retry-After": "1"})

    def wrap(self, name, legacy, *, signature=None):
        return self._legacy.wrap(name, legacy, signature=signature, provider=self._begin)
