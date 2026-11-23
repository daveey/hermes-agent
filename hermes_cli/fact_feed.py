"""Single-owner event/retry scheduling for source captures. [daveey]

No polling: a registered source is read at bootstrap, a native watch hint, an
explicit source deadline, or a failed-capture retry. Host ownership and real
adapter coverage remain separate from this process-neutral scheduler.
"""
from __future__ import annotations

import logging
import contextvars
import threading
import time
from dataclasses import dataclass, field

from hermes_cli.fact_capture import CaptureBudget, CapturePending

log = logging.getLogger(__name__)
_RETRY = (0.1, 0.2, 0.4, 1.0, 2.0, 5.0)



@dataclass(eq=False)
class _Source:
    adapter: object
    cursor: object = None
    due: float | None = None
    retry_at: float | None = None
    failures: int = 0
    active: bool = True
    watch: object = None
    publication: object = field(default_factory=threading.RLock)


class Registration:
    def __init__(self, feed, source):
        self.feed, self.source = feed, source

    def close(self):
        self.feed._remove(self.source)


class ChangeFeed:
    """commit(source, capture) must atomically publish before returning.

    A failed commit does not advance the source cursor. failure(source, error)
    must publish last-good/stale state, not deletion. Callbacks run on this one
    owner thread, never a consumer request thread. Registration is startup-only.
    """
    def __init__(self, commit, failure, *, budget=CaptureBudget()):
        self._commit, self._failure = commit, failure
        self._budget = budget
        self._condition = threading.Condition()
        self._sources = set()
        self._stopping = False
        context = contextvars.copy_context()
        self._thread = threading.Thread(target=lambda: context.run(self._run), name="fact-change-feed", daemon=True)
        self._thread.start()

    def register(self, adapter):
        source = _Source(adapter)
        # Arm first: even a synchronous wake during watch setup is retained.
        source.watch = adapter.watch(lambda: self._wake(source))
        with self._condition:
            if self._stopping:
                source.active = False
            else:
                self._sources.add(source)
                source.due = time.monotonic()
                self._condition.notify_all()
        if not source.active:
            source.watch.close()
            raise RuntimeError("feed stopped")
        return Registration(self, source)

    def _wake(self, source):
        with self._condition:
            if source.active and not self._stopping:
                # Leading edge; more hints cannot postpone it. During capture,
                # this becomes the trailing capture without losing a late write.
                now = time.monotonic()
                source.due = now if source.due is None else min(source.due, now)
                self._condition.notify_all()

    def _remove(self, source):
        with source.publication:
            with self._condition:
                source.active = False
                source.due = None
                self._condition.notify_all()
            # Retain inactive sources until their watch/auxiliary worker really
            # closes. A failed join must remain reachable by owner.close retry.
            if source.watch is not None:
                source.watch.close()
            with self._condition:
                self._sources.discard(source)

    def close(self, timeout=5.0):
        with self._condition:
            self._stopping = True
            sources = tuple(self._sources)
            self._condition.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("source capture did not stop; owner custody retained")
        errors = []
        for source in sources:
            try:
                self._remove(source)
            except Exception as exc:  # health: allow BLE001 -- attempt every cleanup; failed sources remain retained
                errors.append(exc)
        if errors:
            raise errors[0]

    def _next(self):
        with self._condition:
            while not self._stopping:
                candidates = [(max(s.due, s.retry_at or 0), s) for s in self._sources if s.due is not None]
                if not candidates:
                    self._condition.wait()
                    continue
                due, source = min(candidates, key=lambda pair: pair[0])
                delay = due - time.monotonic()
                if delay > 0:
                    self._condition.wait(delay)
                    continue
                source.due = None
                return source
        return None

    def _pass(self, source):
        try:
            capture = source.adapter.capture(source.cursor, self._budget)
            with source.publication:
                with self._condition:
                    if not source.active or self._stopping:
                        return
                # Native hints remain nonblocking during publication.
                self._commit(source.adapter, capture)
                with self._condition:
                    source.cursor = capture.after
                    source.failures = 0
                    source.retry_at = None
                    # Deadlines use monotonic time, not observed_at's UTC clock.
                    deadline = getattr(capture, "next_deadline", None)
                    if deadline is not None:
                        source.due = deadline if source.due is None else min(source.due, deadline)
        except CapturePending:
            with self._condition:
                if source.active and not self._stopping:
                    # Yield to already-due sources, including hints received
                    # while this page ran. Never let a hot source keep its old
                    # leading-edge timestamp across a continuation.
                    source.due = time.monotonic()
                    source.retry_at = None
                    self._condition.notify_all()
        except Exception as exc:
            # Plugin isolation boundary: defects must not kill all sources.
            log.exception("source capture/publication failed; retaining cursor")
            with source.publication:
                with self._condition:
                    if not source.active or self._stopping:
                        return
                    source.retry_at = time.monotonic() + _RETRY[min(source.failures, len(_RETRY) - 1)]
                    source.failures += 1
                    source.due = source.retry_at
                try:
                    self._failure(source.adapter, exc)
                except Exception:
                    log.exception("source health publication failed; retry remains armed")

    def _run(self):
        while (source := self._next()) is not None:
            self._pass(source)
