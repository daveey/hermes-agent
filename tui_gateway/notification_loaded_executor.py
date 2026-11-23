"""f-only cooperative executor for loaded-session notification pollers. [daveey]

With ``display.loaded_notification_f`` ON no chat owns an OS thread. One daemon
thread per completion queue (one per process in production) advances every
loaded session's poller *generator* (``_notification_poller_lane_steps``). A
generator yields ``(timeout, wake_generation)`` exactly where the legacy thread
called ``take_ready(queue, timeout, wake_generation=...)``; this executor waits
on that same queue condition and resumes a lane with ``take_ready(queue, 0)``
once its legacy wait would have returned (queued event, wake generation moved
on, timeout elapsed). The legacy blocking driver runs the same generators, so
completion, producer, parked, held, heartbeat/loop and cleanup obligations are
the same code in both modes.

Per-lane identity is carried explicitly in a ContextVar for consumer-health
receipts. The OS thread registry is never modified. Each step runs in the
lane's own context, isolating the profile runtime scope per session.

Steps are serialized: a lane blocked inside an existing bounded action (a
submit, a board read) delays the other lanes but never drops custody: its
events are in in-flight custody, everyone else's are still queued or held.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
import weakref
from typing import Any

log = logging.getLogger(__name__)

EXECUTOR_THREAD_NAME = "tui-notif-executor"

_current_lane: contextvars.ContextVar[Any] = contextvars.ContextVar("notification_lane", default=None)
_lock = threading.Lock()
_executors: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class Lane(threading.Thread):
    """One loaded session's poller identity. Never started as an OS thread.

    Duck-types the ``threading.Thread`` surface the poller bookkeeping uses
    (``name``, ``ident``, ``is_alive``, ``join``): ``_notification_pollers``
    reapers, ``kanban_consumer_health`` and ``process_consumer_health``.
    """

    _notif_lane = True

    def __init__(self, sid: str, stop: threading.Event):
        super().__init__(name=f"tui-notif-poller-{sid}", daemon=True)
        self.sid = sid
        self.stop_event = stop
        self.context = contextvars.Context()
        self.done = threading.Event()
        self.executor: Executor | None = None
        self.steps: Any = None
        self.factory: Any = None
        self.deadline: float | None = None  # None: never advanced yet, run now
        self.wake_generation = None
        self._admitted_ident: int | None = None

    @property
    def ident(self):
        # None until admitted (like an unstarted thread), then the executor's ident for good,
        # so kanban_consumer_health prunes a finished lane exactly like a dead thread.
        return self._admitted_ident

    def is_alive(self) -> bool:
        return self.executor is not None and not self.done.is_set()

    def start(self):  # pragma: no cover - misuse guard
        raise RuntimeError("a notification lane is advanced by its executor, never started")

    def join(self, timeout=None):
        executor = self.executor
        if executor is not None and executor.thread is not None and threading.get_ident() == executor.thread.ident:
            raise RuntimeError("cannot join a notification lane from its own executor")
        self.done.wait(timeout)

    def _runnable(self, now: float, generation, queued: bool) -> bool:
        # Mirrors take_ready's wait predicate: it returns at once when the queue
        # is non-empty, the timeout is zero, or the wake generation moved on.
        return (self.deadline is None or queued or now >= self.deadline
                or generation != self.wake_generation)


class Executor:
    """Serialized cooperative executor bound to one completion queue's condition."""

    def __init__(self, queue):
        self.queue = queue
        self.lanes: list[Lane] = []
        self.thread: threading.Thread | None = None

    def add(self, lane: Lane, factory) -> None:
        cond = self.queue.not_empty
        with cond:
            lane.executor = self
            lane.factory = factory
            self.lanes.append(lane)
            if self.thread is None:
                # Scope-free by design: every lane step runs in that lane's own context.
                thread = threading.Thread(  # health: allow HX012 -- lanes carry their own contexts
                    target=self._run, name=EXECUTOR_THREAD_NAME, daemon=True)
                self.thread = thread
                thread._notif_executor = self  # type: ignore[attr-defined]
                thread.start()
            lane._admitted_ident = self.thread.ident
            cond.notify_all()

    # -- loop ------------------------------------------------------------------------------------------------------
    def _run(self) -> None:
        me = threading.current_thread()
        cond = self.queue.not_empty
        while True:
            with cond:
                while True:
                    if not self.lanes:
                        if self.thread is me:
                            self.thread = None  # the next add() starts a fresh executor
                        return
                    now = time.monotonic()
                    generation = getattr(self.queue, "_notification_generation", 0)
                    queued = bool(self.queue._qsize())
                    runnable = [lane for lane in self.lanes if lane._runnable(now, generation, queued)]
                    if runnable:
                        break
                    deadlines = [lane.deadline for lane in self.lanes if lane.deadline is not None]
                    cond.wait(max(0.0, min(deadlines) - now) if deadlines else None)
            for lane in runnable:
                if lane.steps is not None:
                    # An earlier lane in this round may have drained the queue: re-check
                    # so a lost race is a skipped no-op, not an extra source pass.
                    with cond:
                        if not lane._runnable(time.monotonic(),
                                              getattr(self.queue, "_notification_generation", 0),
                                              bool(self.queue._qsize())):
                            continue
                self._step(lane)

    def _step(self, lane: Lane) -> None:
        try:
            finished = lane.context.run(self._advance, lane)
        except BaseException:  # health: allow BLE001 -- one lane's failure must not kill every chat's executor
            log.exception("notification lane %s failed", lane.sid)
            finished = True

        if finished:
            with self.queue.not_empty:
                if lane in self.lanes:
                    self.lanes.remove(lane)
            lane.done.set()

    def _advance(self, lane: Lane) -> bool:
        """Advance ``lane`` to its next wait; True once its generator has returned."""
        _current_lane.set(lane)
        try:
            if lane.steps is None:
                lane.steps = lane.factory()
                request = next(lane.steps)
            else:
                from tools.process_registry_awaiting import take_ready
                try:
                    ready = take_ready(self.queue, 0, wake_generation=lane.wake_generation)
                except BaseException as exc:  # health: allow BLE001 -- rethrown into the pass at its wait
                    request = lane.steps.throw(exc)
                else:
                    request = lane.steps.send(ready)
        except StopIteration:
            return True
        timeout, lane.wake_generation = request
        lane.deadline = time.monotonic() + max(0.0, timeout)
        return False


def submit(queue, lane: Lane, factory) -> None:
    """Admit ``lane``; ``factory()`` builds its steps generator inside the lane's context."""
    with _lock:
        executor = _executors.get(queue)
        if executor is None:
            executor = _executors[queue] = Executor(queue)
    executor.add(lane, factory)


def executor_for(queue) -> Executor | None:
    with _lock:
        return _executors.get(queue)


def current_identity():
    """The running poller's identity: its lane under this executor, else the OS thread."""
    consumer = current_consumer()
    return consumer if isinstance(consumer, Lane) else threading.get_ident()


def current_consumer():
    """Explicit health identity, independent of the OS thread registry."""
    lane = _current_lane.get()
    thread = threading.current_thread()
    # Context-carrying child threads must not inherit the lane's publish authority.
    if lane is not None and lane.executor is not None and lane.executor.thread is thread:
        return lane
    return thread
