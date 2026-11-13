"""POSIX control workers, separated along the hosted producer's callback dependency."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Callable


QUIET_BOOKKEEPING_VERBS = ('quiet-observe-start', 'quiet-observe-snapshot', 'quiet-observe-end')
# Read-only identity/status/list polling: passive, never reserved (and never suppressed).
PASSIVE_VERBS = frozenset({
    'identify', 'status', 'profile', 'resolve-profile', 'session-ticket', 'active-work',
    'checkpoint-inventory',
})


def verb_is_work_bearing(verb) -> bool:
    """Default-deny: only an exact known passive/bookkeeping verb avoids reservation. Work-bearing
    verbs (hosted-producer, profile reconcile, plugin reload, callbacks, any new verb) reserve in the
    process-local work registry before scheduling; the executor job's completion is the finalizer."""
    return not (isinstance(verb, str) and (verb in PASSIVE_VERBS or verb in QUIET_BOOKKEEPING_VERBS))


class ControlRequestDispatcher:
    def __init__(self, *, registry=None, home=None) -> None:
        self._registry = registry
        self._home = home
        # Producers synchronously call back into hosted-attest/identify on this listener,
        # then await session work which can itself need the loop's default executor.
        self._controls = ThreadPoolExecutor(thread_name_prefix='gateway-control')
        self._producers = ThreadPoolExecutor(thread_name_prefix='gateway-hosted-producer')

    def _reserve(self, verb):
        """``(registry, token)`` for a work-bearing verb; None when no registry is installed (such a
        gateway is unattested and cannot be measured)."""
        registry = self._registry
        if registry is None:
            from hermes_cli import local_work
            try:
                registry = local_work.get_registry()
            except RuntimeError:
                return None
        home = self._home
        if home is None:
            from gateway.status import _get_process_hermes_home
            home = _get_process_hermes_home()
        return registry, registry.reserve(f'control:{verb}', home=home, source='gateway.control_dispatch')

    async def dispatch(
        self, handler: Callable[[bytes, str | None], bytes], raw: bytes, peer: str | None,
        *, timing=None,
    ) -> bytes:
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            request = None
        # Scheduling is not authorization: the handler still validates the full envelope
        # and authenticated peer before invoking either private operation.
        verb = request.get('verb') if isinstance(request, dict) else None
        producer = verb == 'hosted-producer'
        executor = self._producers if producer else self._controls
        held = self._reserve(verb) if isinstance(request, dict) and verb_is_work_bearing(verb) else None
        # No await between the reservation and the submit: a cancelled waiter cannot strand it.
        try:
            if timing is None:
                future = executor.submit(handler, raw, peer)
            else:
                timing.enqueue()
                future = executor.submit(timing.run, handler, raw, peer)
        except BaseException:
            if held is not None:
                held[0].finish(held[1])
            if timing is not None:
                from concurrent.futures import Future
                failed = Future()
                failed.cancel()
                timing.work_done(failed)
            raise
        if held is not None:
            # Physical finalizer: the executor job ending (or cancelled before it began), never the waiter.
            future.add_done_callback(lambda _future, held=held: held[0].finish(held[1]))
        if timing is not None:
            future.add_done_callback(timing.work_done)
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        # Never run queued mutations after listener shutdown. In-flight handlers retain
        # their existing RPC deadlines; waiting for them here could block the owner loop.
        self._producers.shutdown(wait=False, cancel_futures=True)
        self._controls.shutdown(wait=False, cancel_futures=True)
