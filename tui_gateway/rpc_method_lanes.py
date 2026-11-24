"""[daveey] Per-method concurrency cap for the JSON-RPC worker pool.

The pool is small and shared by every long handler. Without a cap, one slow method that clients keep
re-requesting fills every worker, and unrelated requests wait behind it: on 2026-11-23 the hub backend's
8 workers were all inside ``projects.tree`` (59 s wall each) while ``session.resume`` (0.3 s) timed out
at 30 s for every chat. A method over its cap waits in its own FIFO, holding no worker, and is started
when one of its own calls finishes. Order within a method is kept; nothing is dropped.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

#: Seconds between "at its cap" warnings for one method.
_WARN_INTERVAL_S = 60.0


class MethodLanes:
    """Start at most *cap* calls of one method on the pool at a time (``cap`` None: no limit)."""

    def __init__(self, cap: int | None) -> None:
        self._cap = cap if cap is not None and cap > 0 else None
        self._lock = threading.Lock()
        self._running: dict[str, int] = {}
        self._waiting: dict[str, collections.deque] = {}
        self._warned_at: dict[str, float] = {}

    @property
    def cap(self) -> int | None:
        return self._cap

    def snapshot(self) -> dict[str, dict[str, int]]:
        """``{method: {"running": n, "waiting": m}}`` for methods with a call in flight or queued."""
        with self._lock:
            return {m: {"running": n, "waiting": len(self._waiting.get(m) or ())}
                    for m, n in self._running.items()}

    def run(self, submit: Callable, method: str, fn: Callable[[], object], on_done: Callable[[], object]) -> None:
        """Run ``fn`` through ``submit`` (the pool's), now or once *method* is back under its cap.

        ``on_done`` is called exactly once after ``fn`` finished, was cancelled before it started, or
        could not be handed to the pool later. If the FIRST hand-off raises, that error propagates and
        ``on_done`` is NOT called: the caller still owns its cleanup, as with a bare ``submit``."""
        with self._lock:
            running = self._running.get(method, 0)
            if self._cap is not None and running >= self._cap:
                queue = self._waiting.setdefault(method, collections.deque())
                queue.append((submit, fn, on_done))
                depth, warn = len(queue), self._should_warn(method)
            else:
                self._running[method] = running + 1
                depth, warn = 0, False
        if depth:
            if warn:
                logger.warning("rpc pool: %s is at its cap of %d running calls; %d waiting behind it",
                               method, self._cap, depth)
            return
        try:
            future = submit(fn)
        except BaseException:
            self._finished(method)
            raise
        future.add_done_callback(lambda done: self._done(method, on_done, done))

    def _should_warn(self, method: str) -> bool:  # under the lock
        now = time.monotonic()
        if now - self._warned_at.get(method, float("-inf")) < _WARN_INTERVAL_S:
            return False
        self._warned_at[method] = now
        return True

    def _done(self, method: str, on_done: Callable[[], object], future) -> None:
        try:
            on_done()
        finally:
            if future.cancelled():
                # A cancel runs this callback on the cancelling thread. ``shutdown(cancel_futures=True)``
                # cancels while holding the executor's own lock, and ``submit`` takes that lock: handing
                # the slot over inline would deadlock the shutdown. Hand over from a thread of its own.
                try:
                    threading.Thread(  # health: allow HX012 -- hand-off only: each call runs in the context it captured at dispatch
                        target=self._finished, args=(method,), name="tui-rpc-lane-handoff", daemon=True).start()
                except RuntimeError:  # no thread to be had (interpreter exit, thread limit): release, start nothing
                    logger.warning("rpc pool: no hand-off thread for %s; releasing its waiting calls unstarted",
                                   method, exc_info=True)
                    self._finished(method, start=False)
            else:
                self._finished(method)

    def _finished(self, method: str, start: bool = True) -> None:
        """One call of *method* left the pool: hand its slot to the next waiting call, else free it.
        ``start=False`` (no thread from which ``submit`` is safe): the waiting calls are released
        unstarted, each with its done hook, and the slot is freed."""
        interrupt: BaseException | None = None  # a non-Exception from ``submit``: re-raised once the lane is settled
        while True:
            with self._lock:
                queue = self._waiting.get(method)
                if not queue:
                    self._waiting.pop(method, None)
                    left = self._running.get(method, 0) - 1
                    if left > 0:
                        self._running[method] = left
                    else:
                        self._running.pop(method, None)
                        self._warned_at.pop(method, None)
                    if interrupt is not None:
                        raise interrupt
                    return
                submit, fn, on_done = queue.popleft()
            try:
                if not start:
                    raise RuntimeError("no thread available to start a waiting call")
                future = submit(fn)
            except BaseException as exc:  # the pool is shut down: the call never runs, its caller's cleanup still does
                logger.warning("rpc pool: could not start a waiting %s call", method, exc_info=True)
                if not isinstance(exc, Exception) and interrupt is None:
                    interrupt = exc
                try:
                    on_done()
                except Exception:
                    logger.warning("rpc pool: cleanup failed for an unstarted %s call", method, exc_info=True)
                continue
            future.add_done_callback(lambda done, hook=on_done: self._done(method, hook, done))
            if interrupt is not None:
                raise interrupt
            return
