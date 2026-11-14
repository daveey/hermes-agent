"""Deterministic scheduling at the real poller's blocking queue boundary.

No production subpoll is replaced. Only queue waiting is virtual: each waiter
uses the actual production due_timeout, and real take_ready keeps event custody.
Single-file benchmark callers must finish all workers before undoing patches.
"""
import threading
import time
from collections import defaultdict


class PollerScheduler:
    def __init__(self, clock, real_take):
        self.clock = clock
        self.real_take = real_take
        self.condition = threading.Condition()
        self.waiting = {}
        self.permits = set()
        self.errors = []
        self.threads = []
        self.stops = []
        self.samples = defaultdict(list)
        self.timeouts = defaultdict(list)
        self.starts = {}
        self.closed = False

    def take(self, queue, timeout, **kwargs):
        ident = threading.get_ident()
        ready = self.real_take(queue, 0, **kwargs)
        if ready or timeout <= 0 or self.closed:
            return ready
        with self.condition:
            self.samples[ident].append(time.thread_time() - self.starts[ident])
            self.timeouts[ident].append(timeout)
            self.waiting[ident] = self.clock[0] + timeout
            self.condition.notify_all()
            assert self.condition.wait_for(lambda: ident in self.permits or self.closed, 30), "scheduler stalled"
            self.permits.discard(ident)
            self.waiting.pop(ident, None)
        self.starts[ident] = time.thread_time()
        return self.real_take(queue, 0, **kwargs)

    def start(self, target, stop, sid, session):
        def run():
            ident = threading.get_ident()
            self.starts[ident] = time.thread_time()
            try:
                target(stop, sid, session)
            except BaseException as exc:
                self.errors.append(repr(exc))
            finally:
                with self.condition:
                    self.samples[ident].append(time.thread_time() - self.starts[ident])
                    self.condition.notify_all()
        thread = threading.Thread(target=run, name=f"replay-{sid}", daemon=True)
        self.threads.append(thread)
        self.stops.append(stop)
        thread.start()
        with self.condition:
            assert self.condition.wait_for(lambda: thread.ident in self.waiting or self.errors, 30)
            assert not self.errors, self.errors

    def advance(self, now):
        # No worker runs while the controller changes inputs and the shared clock.
        with self.condition:
            assert len(self.waiting) == len(self.threads)
            self.clock[0] = now
            due = [ident for ident, deadline in self.waiting.items() if deadline <= now]
            for ident in due:
                before = len(self.samples[ident])
                self.permits.add(ident)
                self.condition.notify_all()
                assert self.condition.wait_for(lambda: len(self.samples[ident]) > before or self.errors, 30)
                assert not self.errors, self.errors

    def close(self):
        for stop in self.stops:
            stop.set()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(30)
            assert not thread.is_alive(), thread.name
        assert not self.errors, self.errors
