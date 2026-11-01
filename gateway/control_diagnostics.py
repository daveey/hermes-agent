"""Local control diagnostics; never retains request payloads or stack locals."""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
import logging
import threading
import time

logger = logging.getLogger(__name__)


def safe_verb(raw, allowed):
    try:
        request = json.loads(raw)
        verb = request.get("verb") if isinstance(request, dict) else None
        return verb if isinstance(verb, str) and verb in allowed else "unknown"
    except (ValueError, UnicodeError):
        return "unknown"


class ControlDiagnostics:
    def __init__(self):
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self):
        with self._lock:
            return self._in_flight

    def accepted(self):
        # Acceptance means the asyncio connection callback, NOT kernel accept.
        with self._lock:
            self._in_flight += 1
        return RequestTiming(self, time.monotonic())

    def completed(self, timing):
        with self._lock:
            count = self._in_flight
            self._in_flight -= 1
        if timing.total > 0.5 or timing.queue_wait > 0.5:
            fields = dict(verb=timing.verb, queue_wait_ms=timing.queue_wait * 1000,
                          run_ms=timing.run_time * 1000, total_ms=timing.total * 1000,
                          in_flight=count)
            logger.warning(
                "Control request latency (callback acceptance-to-drain/completion) "
                "verb=%s queue_wait_ms=%.1f run_ms=%.1f total_ms=%.1f in_flight=%d",
                *fields.values(), extra=fields)


class RequestTiming:
    def __init__(self, owner, accepted):
        self.owner, self.accepted = owner, accepted
        self.verb = "unknown"
        self.queue_wait = self.run_time = self.total = 0.0
        self._lock = threading.Lock()
        self._connection_done = self._work_pending = self._reported = False

    def enqueue(self):
        with self._lock:
            self._work_pending = True
            self._enqueued = time.monotonic()

    def run(self, handler, *args):
        started = time.monotonic()
        with self._lock:
            self.queue_wait = started - self._enqueued
        try:
            return handler(*args)
        finally:
            with self._lock:
                self.run_time = time.monotonic() - started

    def work_done(self, future):
        with self._lock:
            if future.cancelled():
                self.queue_wait = time.monotonic() - self._enqueued
            self._work_pending = False
            # A cancelled connection cannot cancel an already-running Python handler.
            # Its completion, rather than the abandoned drain, ends total latency.
            self.total = max(self.total, time.monotonic() - self.accepted)
            self._report_if_done()

    def finish(self):
        with self._lock:
            if self._connection_done:
                return False
            self.total = time.monotonic() - self.accepted
            self._connection_done = True
            self._report_if_done()
            return True

    def _report_if_done(self):
        # Cancelled connections remain in flight until any executing handler ends.
        if self._connection_done and not self._work_pending and not self._reported:
            self._reported = True
            self.owner.completed(self)


def _frame_location(frame):
    # No locals, source lines, task reprs or names: they may contain request secrets.
    return f"{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"


def _thread_stacks():
    stacks = []
    for ident, frame in sys._current_frames().items():
        locations = []
        for _ in range(64):
            if frame is None:
                break
            locations.append(_frame_location(frame))
            frame = frame.f_back
        stacks.append(f"thread={ident}\n" + "\n".join(locations))
    return "\n".join(stacks)


class LoopWatchdog:
    """Diagnostic only. Live thread stacks plus the last loop-owned task snapshot.

    A loop blocked in Python/native code must release the GIL for this thread to run.
    This does not replace or change the long-timeout process liveness watchdog.
    """
    interval = 0.25
    threshold = 1.0
    repeat_interval = 30.0

    def __init__(self, loop):
        self.loop = loop
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_warning = float("-inf")
        self._heartbeat = None
        from agent.memory_provider import spawn_context_thread
        self.thread = spawn_context_thread(self._watch, name="control-loop-diagnostics", daemon=True)

    def start(self):
        self._beat()
        self.thread.start()

    def _beat(self):
        if self._stop.is_set():
            return
        # asyncio task enumeration and get_stack are ONLY called on their owning loop.
        stacks = "\n".join(
            "task\n" + "\n".join(_frame_location(f) for f in task.get_stack(limit=16))
            for task in itertools.islice(asyncio.all_tasks(self.loop), 64))
        with self._lock:
            self._snapshot_at = time.monotonic()
            self._due = self._snapshot_at + self.interval
            self._tasks = stacks
        self._heartbeat = self.loop.call_later(self.interval, self._beat)

    def _watch(self):
        while not self._stop.wait(self.interval):
            self._check()

    def _check(self):
        now = time.monotonic()
        with self._lock:
            delay, tasks, age = now - self._due, self._tasks, now - self._snapshot_at
        if delay <= self.threshold or now - self._last_warning < self.repeat_interval:
            return
        self._last_warning = now
        threads = _thread_stacks()
        if self._stop.is_set():
            return
        logger.warning(
            "Control event loop blocked delay_ms=%.1f; live thread stacks:\n%s\n"
            "last loop-owned task snapshot age_ms=%.1f:\n%s",
            delay * 1000, threads, age * 1000, tasks,
            extra=dict(delay_ms=delay * 1000, thread_stacks=threads, task_stacks=tasks,
                       task_snapshot_age_ms=age * 1000))

    def stop(self):
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)

