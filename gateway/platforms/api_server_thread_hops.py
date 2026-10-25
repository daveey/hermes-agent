"""Worker-thread hops onto the owning event loop, and the SSE queue built on them. [daveey]

Split out of ``api_server`` (code-health file cap). ``api_server`` re-exports both names.
"""

from __future__ import annotations

import asyncio
import threading
import time

__all__ = ["ThreadSafeAsyncQueue", "_ThreadHops"]


class _ThreadHops:
    """Tracks ``call_soon_threadsafe`` hops from worker threads onto the owning loop.

    A hop scheduled by the executor thread is NOT guaranteed to run before the loop observes
    that same thread's ``run_in_executor`` future as done (reproduced on CPython 3.14.7: roughly
    1 in 7000 under load). A terminal event or EOS sentinel enqueued right after the worker
    returns could therefore overtake the worker's last event, which the SSE writer then never
    sends. The counter is bumped synchronously in the worker thread, so after the worker returns
    ``flush_hops`` waits for every hop it scheduled.
    """

    _hops_pending = 0

    def _init_hops(self) -> None:
        self._hops_lock = threading.Lock()
        self._hops_pending = 0

    def _hop(self, loop: asyncio.AbstractEventLoop, fn, *args) -> None:
        with self._hops_lock:
            self._hops_pending += 1

        def _run() -> None:
            try:
                fn(*args)
            finally:
                with self._hops_lock:
                    self._hops_pending -= 1

        try:
            loop.call_soon_threadsafe(_run)
        except BaseException:
            with self._hops_lock:
                self._hops_pending -= 1
            raise

    async def flush_hops(self, timeout: float = 5.0) -> None:
        """Wait (bounded) until every hop scheduled so far has run on the loop."""
        deadline = time.monotonic() + timeout
        spins = 0
        while self._hops_pending > 0 and time.monotonic() < deadline:
            spins += 1
            await asyncio.sleep(0 if spins < 50 else 0.002)

    def call_when_flushed(self, loop: asyncio.AbstractEventLoop, fn, *args, timeout: float = 5.0) -> None:
        """Run ``fn(*args)`` on ``loop`` once every hop scheduled so far has run (bounded).

        For sync callbacks (e.g. a task done-callback) that cannot ``await flush_hops()``:
        re-checks with ``call_later`` instead of spawning a task nobody holds."""
        deadline = time.monotonic() + timeout

        def _check() -> None:
            if self._hops_pending > 0 and time.monotonic() < deadline:
                loop.call_later(0.002, _check)
                return
            fn(*args)

        _check()


class ThreadSafeAsyncQueue(_ThreadHops, asyncio.Queue):
    """``asyncio.Queue`` a non-loop thread (run_conversation's executor) can push into via
    ``put_threadsafe``; the SSE consumer's ``await get()`` is woken by ``call_soon_threadsafe``.
    Writers must ``await flush_hops()`` once the worker is done and before enqueueing EOS."""

    def put_threadsafe(self, item, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._hop(loop or self._loop_ref, self.put_nowait, item)

    def put_after_hops(self, item) -> None:
        """``put_nowait(item)`` once pending worker hops have landed (call on the loop)."""
        self.call_when_flushed(self._loop_ref, self.put_nowait, item)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_hops()
        # Always constructed inside a running async handler (the SSE
        # request handlers below), so get_running_loop() is safe here.
        self._loop_ref = asyncio.get_running_loop()
