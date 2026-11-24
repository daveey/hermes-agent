"""Scoped, inert board view consumer of the published ``ctx.facts.v1`` API.

The authorized host owns binding and the all-consumer release latch. This module
never opens a source, reads configuration or acquires a projection owner. A cold
selector is queued through the host's memory-only demand channel, not hydrated by
the request. Legacy readers remain installed for coordinated rollback.
"""
from __future__ import annotations

import functools
import inspect
import json
import threading
from contextlib import contextmanager, nullcontext

from fastapi import HTTPException
from fastapi.responses import Response
from collections.abc import Callable, Mapping, Sequence
from hermes_cli.projection_request import RequestBinding


class BoardViews:
    """One authorized scope's board, board switcher and project catalog views."""

    def __init__(self, v1, scope, *, stop_legacy: Callable, start_legacy: Callable,
                 demand: Callable, hot_selectors: Sequence[tuple[str, Mapping]], lifecycle=None):
        self.v1 = v1
        self.scope = scope
        self._stop_legacy = stop_legacy
        self._start_legacy = start_legacy
        self._demand = demand
        self._hot = tuple((name, dict(selector)) for name, selector in hot_selectors)
        self._transition = threading.RLock()
        self._readers = threading.Condition()
        self._active = 0
        self._mode = "legacy"
        self._generation = -1
        self._closed = False
        self._subscription = None
        # ReplicaV1 owns reads, not the coordinator's global release authority.
        self._lifecycle = v1 if lifecycle is None else lifecycle
        self._registration = self._lifecycle.bind_consumer("board", scope, self.on_mode)
        # Readiness observes publication while legacy still serves; otherwise a
        # cold hot-selector can never make the all-consumer latch ready.
        self._readiness = v1.subscribe(scope, ("ViewFact",), None, None, self._changed)

    def prepare(self) -> bool:
        """Off-request prewarm/readiness check; absence is never an empty board."""
        # Publication callbacks can already be queued when close begins. Keep
        # demand/readiness in the same lifecycle critical section as unregister.
        with self._transition:
            if self._closed:
                return False
            snapshot = self.v1.snapshot(self.scope)
            ready = bool(self._hot)
            for name, selector in self._hot:
                result = snapshot.view(name, selector)
                if not isinstance(result, self.v1.Ready):
                    ready = False
                    self._demand(name, selector)
            self._lifecycle.report_ready("board", self.scope, version=1, ready=ready)
            return ready

    def _changed(self, _delta) -> None:
        self.prepare()

    def on_mode(self, mode: str, generation: int) -> None:
        """Latch callback, never a request-time config check.

        Withdraw the legacy path before draining it; a failed drain leaves requests
        unavailable, not silently running a second reader. The host must retry or
        roll back the entire release if any callback fails.
        """
        if mode not in {"legacy", "facts"}:
            raise ValueError("unsupported board consumer mode")
        with self._transition:
            if self._closed or generation < self._generation:
                return
            if generation == self._generation and mode == self._mode:
                return
            initial = self._generation < 0
            # Fence attempted generations too: a failed drain must not permit an
            # older callback to reopen admission. Same-generation retry is valid.
            with self._readers:
                self._generation = generation
            if mode == "facts":
                if self._mode != "facts":
                    with self._readers:
                        self._mode = "transition"
                        if not self._readers.wait_for(lambda: self._active == 0, 5.0):
                            raise TimeoutError("legacy board requests did not drain")
                    self._stop_legacy()
                    self._subscription = self.v1.subscribe(
                        self.scope, ("ViewFact",), None, None, self._changed)
                    self._mode = "facts"
            elif self._mode != "legacy" or initial:
                with self._readers:
                    self._mode = "transition"
                if self._subscription is not None:
                    self._subscription.close()
                    self._subscription = None
                self._start_legacy()
                self._mode = "legacy"
            self._generation = generation

    @contextmanager
    def legacy(self):
        """Lease held by the actual synchronous reader, not its cancellable waiter."""
        with self._readers:
            if self._closed or self._mode != "legacy":
                raise HTTPException(503, detail={"code": "facts_unavailable", "reason": "board_transition"},
                                    headers={"Retry-After": "1"})
            self._active += 1
        try:
            yield
        finally:
            with self._readers:
                self._active -= 1
                self._readers.notify_all()

    def read(self, view: str, selector: Mapping):
        """None means legacy; all other results prohibit synchronous fallback."""
        with self._readers:
            mode, generation = self._mode, self._generation
            if mode == "legacy" and not self._closed:
                return None
            if mode != "facts" or self._closed:
                return self.v1.Unavailable("board_transition")
        result = self.v1.snapshot(self.scope).view(view, selector)
        # Snapshot lookup is memory-only, but shutdown can still interleave with
        # it. Demand must not touch a closed handle or a superseded release.
        with self._readers:
            if self._closed or self._mode != "facts" or self._generation != generation:
                return self.v1.Unavailable("board_transition")
            if isinstance(result, self.v1.Pending):
                self._demand(view, selector)
            return result

    def close(self) -> None:
        """Shutdown registration and subscription; never restart legacy on close."""
        with self._transition:
            if self._closed:
                return
            needs_stop = self._mode != "facts"
            with self._readers:
                self._mode = "transition"
                if not self._readers.wait_for(lambda: self._active == 0, 5.0):
                    raise TimeoutError("legacy board shutdown did not drain")
            if needs_stop:
                self._stop_legacy()
            self._closed = True
            self._registration.close()
            self._readiness.close()
            if self._subscription is not None:
                self._subscription.close()
                self._subscription = None


_installed = None
_request = RequestBinding("board_fact_request")


def install_provider(v1, provider) -> None:
    """Startup injection; provider(board) resolves only authorized in-memory scope.

    It returns a BoardViews, an Unavailable result, or None for selected legacy.
    Missing facts-mode bindings MUST return Unavailable, never None.
    """
    global _installed
    _installed = (v1, provider)


def response(view: str, selector: Mapping, *, board=None, decoded=False):
    """Route fast path before selection, coalescing, disk access or encoding."""
    selected = _request.get(board, _installed)
    if selected is None:
        return None
    v1, bound = selected
    if bound is None:
        return None
    # A scope still activating is Pending (BoardRuntime.provider): not a view, so it answers 503 like Unavailable.
    result = bound if isinstance(bound, (v1.Unavailable, v1.Pending)) else bound.read(view, selector)
    if result is None:
        return None
    if isinstance(result, v1.Ready):
        if decoded:
            return json.loads(result.encoded)
        return Response(content=result.encoded, media_type="application/json")
    raise HTTPException(status_code=503, detail={"code": "facts_unavailable", "reason": result.reason},
                        headers={"Retry-After": "1"})


def route(view: str, *, decoded=False):
    """Preserve the exported callable's signature and bypass its entire legacy body."""
    def decorate(function):
        signature = inspect.signature(function)

        @contextmanager
        def lookup(args, kwargs):
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            # FastAPI supplies values; direct Python callers can omit Query defaults.
            values = {key: getattr(value, "default", value)
                      for key, value in arguments.arguments.items()}
            board = values.pop("board", None)
            with _request.pin(board, _installed) as selected:
                bound = selected[1] if selected is not None else None
                canonical = bound.scope.board_id if isinstance(bound, BoardViews) else board
                # Bind legacy selection too: a current-board change must not move
                # the actual read away from the adapter whose lease we acquired.
                if "board" in arguments.arguments:
                    if bound is None and board is None:
                        # Only the explicitly selected legacy path may discover
                        # its current board. Freeze it before the detached read.
                        from hermes_cli import kanban_db
                        canonical = kanban_db.get_current_board()
                    arguments.arguments["board"] = canonical
                with _request.alias(canonical):
                    yield response(view, values, board=board, decoded=decoded), board, arguments

        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                with lookup(args, kwargs) as (result, board, arguments):
                    if result is not None:
                        return result
                    with legacy(board):
                        return await function(*arguments.args, **arguments.kwargs)
            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            with lookup(args, kwargs) as (result, board, arguments):
                if result is not None:
                    return result
                with legacy(board):
                    return function(*arguments.args, **arguments.kwargs)
        return synchronous
    return decorate


def legacy(board):
    """Startup provider retains the bound adapter in legacy mode for draining."""
    selected = _request.get(board, _installed)
    if selected is None:
        return nullcontext()
    v1, bound = selected
    if bound is None:
        return nullcontext()
    if isinstance(bound, (v1.Unavailable, v1.Pending)):
        raise HTTPException(503, detail={"code": "facts_unavailable", "reason": bound.reason})
    return bound.legacy()


def bind_api(ctx, api, *, provider, stop_legacy, hot_selectors, legacy_paths, lifecycle=None):
    """Dashboard startup after authorized context injection (producer-owned mount)."""
    from plugins.kanban.dashboard import board_legacy, board_rows, board_snapshot
    from plugins.kanban.dashboard.board_prewarm import start_board_prewarm, stop_board_prewarm
    paths = tuple(map(str, legacy_paths))

    def stop():
        stop_legacy()  # host drains scoped routes (including detached coalesced jobs)
        stop_board_prewarm()
        board_legacy.stop(paths)
        board_rows.retire(paths)
        board_snapshot.retire(paths)

    def start():
        board_legacy.resume(paths)
        start_board_prewarm(api)

    install_provider(ctx.facts.v1, provider)
    return bind(ctx.facts.v1, ctx.facts.scope, stop_legacy=stop,
                start_legacy=start, demand=ctx.facts.handle.demand,
                hot_selectors=hot_selectors, lifecycle=lifecycle)


def bind(v1, scope, **kwargs) -> BoardViews:
    """Called by the authorized host at startup, not by route handlers."""
    return BoardViews(v1, scope, **kwargs)


def board_selector(tenant=None, include_archived=False, workflow_template_id=None,
                   current_step_key=None, done_limit=80) -> dict:
    """The board identity lives in Scope; every projection argument stays keyed."""
    return {"tenant": tenant, "include_archived": bool(include_archived),
            "workflow_template_id": workflow_template_id, "current_step_key": current_step_key,
            "done_limit": int(done_limit or 0)}
