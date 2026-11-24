"""Bounded thread lanes for dashboard routes. [daveey]

Every synchronous route and ``run_in_threadpool`` call shares AnyIO's default limiter (40 worker
threads). On 2026-11-21 the hub's always-on backend spent ~2 h with that pool full of session and kanban
reads waiting on locks (``/changes`` up to 926 s): ``/api/status`` queued behind them for up to 632 s,
Desktop gave up at 30 s and retried, and the backend kept running reads nobody was waiting for.

A lane is a group of routes with its own thread budget:

* ``control`` -- the thread work of ``/api/status`` and Desktop's small control reads. Never a kanban or
  ``state.db`` read: those may wait on a lock, and this lane must stay free.
* ``sessions`` -- session lists and project trees.
* ``kanban`` -- linked-card reads across every board (``/api/session-kanban-tasks``): polled per sidebar row and
  slow when a board is busy, so they never take the session lists' threads.
* ``probe`` -- the short, bounded ``state.db`` reads inside ``/api/status`` (storage probe, active-session
  count, FTS rebuild status), with a short deadline: they must not wait behind session lists, and a busy
  probe lane only marks those fields unknown/degraded instead of holding the status answer.
* ``feed`` -- plugin catch-up reads (``session-panel`` ``/changes``, ``user-steps``).
* ``stripe`` -- Desktop paint/reason diagnostics, including DB reads and JSONL comparison. A paint burst
  neither consumes the default executor nor queues behind session lists, feeds or control reads.

A full lane holds only its own threads, never the shared pool. A caller waits at most ``queue_deadline_s``
(below Desktop's 30 s timeout) for one of them; then, or as soon as its client has disconnected (callers that
pass ``request=``), it gets ``503`` with ``Retry-After`` and the work never starts, so a retry is always safe.
Work that already started runs to the end (a Python thread cannot be interrupted safely) and keeps its lane
slot until its thread returns, even when the caller was cancelled meanwhile: a lane never runs more threads
than its size. A ``coalesced_read`` caller first waits on that wrapper's own admission (no deadline there),
then on the lane.

Sizes and the deadline come from ``dashboard.thread_lanes`` in config.yaml, read once at app start-up
(``configure()`` in a worker thread); until then the built-in defaults apply.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
import weakref
from typing import Any, Callable, Optional

import anyio
import anyio.to_thread as _to_thread  # looked up per call: loop accounting wraps ``run_sync`` on the module
from fastapi import HTTPException

_log = logging.getLogger(__name__)

DEFAULT_SIZES = {"control": 4, "sessions": 8, "kanban": 4, "feed": 6, "probe": 2, "stripe": 2}
QUEUE_DEADLINE_S = 25.0      # below Desktop's 30 s request timeout: a waiter it gave up on never runs
RETRY_AFTER_S = 2
DISCONNECT_POLL_S = 1.0
REJECT_LOG_EVERY_S = 10.0


class LaneBusy(HTTPException):
    """503: the lane had no free thread within the deadline (or the client left). The work never started."""

    def __init__(self, lane: str, reason: str, waited_s: float):
        super().__init__(status_code=503, headers={"Retry-After": str(RETRY_AFTER_S)},
                         detail={"error": "lane_busy", "lane": lane, "reason": reason,
                                 "waited_s": round(waited_s, 2)})
        self.lane, self.reason, self.waited_s = lane, reason, waited_s


class _Lane:
    def __init__(self, name: str, size: int):
        self.name, self.size = name, int(size)
        self._limiters: weakref.WeakKeyDictionary[Any, tuple[anyio.CapacityLimiter, anyio.CapacityLimiter]] = (
            weakref.WeakKeyDictionary())
        self._lock = threading.Lock()
        self.in_use = self.waiting = self.served = self.rejected = self.disconnected = 0
        self.max_wait_s = 0.0
        self._last_reject_log = 0.0

    def limiters(self) -> tuple[anyio.CapacityLimiter, anyio.CapacityLimiter]:
        """(gate, runner) for the running event loop. The gate is what callers queue on (with a deadline);
        the runner, the same size, is what the threads run under, so it never makes an admitted caller wait
        and never touches the shared default limiter."""
        import asyncio
        loop = asyncio.get_running_loop()
        with self._lock:
            pair = self._limiters.get(loop)
            if pair is None:
                pair = (anyio.CapacityLimiter(self.size), anyio.CapacityLimiter(self.size))
                self._limiters[loop] = pair
            return pair

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"size": self.size, "in_use": self.in_use, "waiting": self.waiting, "served": self.served,
                    "rejected": self.rejected, "disconnected": self.disconnected,
                    "max_wait_s": round(self.max_wait_s, 3)}

    def _add(self, **deltas: int) -> None:
        with self._lock:
            for key, value in deltas.items():
                setattr(self, key, getattr(self, key) + value)

    def _waited(self, seconds: float) -> None:
        with self._lock:
            self.max_wait_s = max(self.max_wait_s, seconds)

    def _should_log_reject(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last_reject_log < REJECT_LOG_EVERY_S:
                return False
            self._last_reject_log = now
            return True


_lanes: dict[str, _Lane] = {}
_deadline_s: Optional[float] = None
_config_lock = threading.Lock()


def _read_config() -> tuple[dict[str, int], float]:
    sizes, deadline = dict(DEFAULT_SIZES), QUEUE_DEADLINE_S
    try:
        from hermes_cli.config import load_config
        section = ((load_config().get("dashboard") or {}).get("thread_lanes") or {})
    except BaseException:  # health: allow BLE001 -- ConfigBackendUnavailable is a SystemExit; keep the defaults
        _log.debug("thread lanes: config unreadable; using defaults", exc_info=True)
        return sizes, deadline
    if not isinstance(section, dict):
        return sizes, deadline
    for name in sizes:
        value = section.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            sizes[name] = value
    value = section.get("queue_deadline_s")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        deadline = float(value)
    return sizes, deadline


def configure(sizes: Optional[dict[str, int]] = None, queue_deadline_s: Optional[float] = None) -> None:
    """Set the lanes (``None``: read config.yaml). Resets every counter; for start-up and tests. The new
    table replaces the old one in one assignment, so a concurrent lookup never sees it half-built."""
    global _deadline_s, _lanes
    read_sizes, read_deadline = _read_config() if sizes is None or queue_deadline_s is None else ({}, 0.0)
    sizes = dict(sizes) if sizes is not None else read_sizes
    table = {name: _Lane(name, size) for name, size in sizes.items()}
    with _config_lock:
        _lanes = table
        _deadline_s = float(queue_deadline_s) if queue_deadline_s is not None else read_deadline


def _lane(name: str) -> _Lane:
    # Never read config.yaml here (this runs on the event loop): the app's start-up calls configure() in a
    # thread; before that, or in a process that never does, the built-in defaults apply.
    if _deadline_s is None:
        with _config_lock:
            needed = _deadline_s is None
        if needed:
            configure(DEFAULT_SIZES, QUEUE_DEADLINE_S)
    lane = _lanes.get(name)
    if lane is None:
        raise KeyError(f"unknown thread lane {name!r}; known: {sorted(_lanes)}")
    return lane


def queue_deadline_s() -> float:
    _lane(next(iter(DEFAULT_SIZES)))
    return float(_deadline_s or QUEUE_DEADLINE_S)


async def _admit(gate: anyio.CapacityLimiter, token: object, deadline: float, request: Any) -> str:
    """'ok' with ``token`` holding one gate slot; else 'deadline' or 'disconnected' with nothing held.

    The caller's own cancellation can land at any point, including right after the slot was handed over:
    any exception releases a slot already taken before it propagates. A disconnect noticed after the slot
    was taken is ignored (the call runs; nothing leaks)."""
    acquired = False
    gone = False
    task = asyncio.current_task()
    cancels_before = task.cancelling() if task is not None else 0
    try:
        # A free slot is taken without awaiting: anyio's uncontended acquire passes a shielded checkpoint, and a
        # native cancel landing there makes it release on behalf of the wrong borrower (RuntimeError, slot
        # leaked). Only a contended wait awaits, and that path releases correctly when cancelled.
        try:
            gate.acquire_on_behalf_of_nowait(token)
            acquired = True
        except anyio.WouldBlock:
            pass
        if acquired:
            return "ok"
        with anyio.move_on_after(deadline):
            if request is None:
                await gate.acquire_on_behalf_of(token)
                acquired = True
            else:
                async with anyio.create_task_group() as tg:
                    async def watch() -> None:
                        nonlocal gone
                        while not acquired:
                            await anyio.sleep(DISCONNECT_POLL_S)
                            if acquired:
                                return
                            if await request.is_disconnected() and not acquired:
                                gone = True
                                tg.cancel_scope.cancel()
                                return

                    tg.start_soon(watch)
                    await gate.acquire_on_behalf_of(token)
                    acquired = True
                    tg.cancel_scope.cancel()
                # A native cancel of this task that the task group's own (already cancelled) scope absorbed
                # must still reach the caller.
                if task is not None and task.cancelling() > cancels_before:
                    raise asyncio.CancelledError()
    except BaseException:
        if acquired:
            gate.release_on_behalf_of(token)
        raise
    if acquired:
        return "ok"
    return "disconnected" if gone else "deadline"


async def run_in_lane(lane: str, fn: Callable[..., Any], *args: Any, request: Any = None,
                      deadline: Optional[float] = None, **kwargs: Any) -> Any:
    """``run_in_threadpool`` on a lane: wait at most the queue deadline (or a shorter ``deadline`` for this
    call) for one of the lane's threads, then run ``fn(*args, **kwargs)`` there. Raises :class:`LaneBusy`
    (503) without running ``fn`` when no thread freed up in time, or when ``request`` disconnected while
    waiting. Pass ``request`` only where the body is already read (GET, or a parsed body parameter):
    ``Request.is_disconnected()`` drops body chunks it receives."""
    entry = _lane(lane)
    limit = queue_deadline_s() if deadline is None else min(float(deadline), queue_deadline_s())
    gate, runner = entry.limiters()
    token = object()
    started = time.monotonic()
    entry._add(waiting=1)
    try:
        outcome = await _admit(gate, token, limit, request)
    finally:
        entry._add(waiting=-1)
    waited = time.monotonic() - started
    entry._waited(waited)
    if outcome != "ok":
        entry._add(**({"disconnected": 1} if outcome == "disconnected" else {"rejected": 1}))
        if outcome == "deadline" and entry._should_log_reject():
            _log.warning("thread lane %s full: refused a call after %.1fs (%s)", lane, waited, entry.stats())
        raise LaneBusy(lane, outcome, waited)

    # The slot is released when the work is over, not when the caller stops waiting: a cancelled caller
    # (e.g. /api/status's own timeout) leaves its thread running, and that thread still counts.
    loop = asyncio.get_running_loop()
    state_lock = threading.Lock()
    state = {"entered": False, "skipped": False, "done": False}

    def finish(ran: bool = True) -> None:
        with state_lock:
            if state["done"]:
                return
            state["done"] = True
        entry._add(in_use=-1, served=1 if ran else 0)  # served: calls whose work ran
        gate.release_on_behalf_of(token)

    def call() -> Any:
        with state_lock:
            if state["skipped"]:  # the caller was cancelled before this thread picked the work up
                return None
            state["entered"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:  # the loop is closed: nothing is left to release
                pass

    functools.update_wrapper(call, getattr(fn, "func", fn))  # loop accounting names the thread after fn
    entry._add(in_use=1)
    try:
        result = await _to_thread.run_sync(call, limiter=runner)
    except BaseException as exc:
        with state_lock:
            entered = state["entered"]
            if not entered:
                state["skipped"] = True
        # Not started (now never will) or already finished (fn raised): release now. Started and the caller
        # was cancelled: the thread releases when it returns.
        if not entered:
            finish(ran=False)
        elif not isinstance(exc, (asyncio.CancelledError, anyio.get_cancelled_exc_class())):
            finish()
        raise
    finish()
    return result


def lane_runner(lane: str) -> Callable[[Callable[[], Any]], Any]:
    """A ``coalesced_read(thread_runner=...)`` that runs the shared read on ``lane``."""
    async def run(func: Callable[[], Any]) -> Any:
        return await run_in_lane(lane, func)
    run.__name__ = f"lane_runner_{lane}"
    return run


def stats() -> dict[str, Any]:
    """Per-lane counters for ``/api/health`` (counts only: no paths or arguments)."""
    deadline = queue_deadline_s()
    return {"queue_deadline_s": deadline, **{name: lane.stats() for name, lane in sorted(_lanes.items())}}
