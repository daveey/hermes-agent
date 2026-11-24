"""Cooperative timer service inside a serial, single-writer dispatch tick.

Checkpoints do not preempt a slow call. The service interval bounds scan frequency,
not wall-clock wake latency: a blocking phase (or the wake scan itself) can still
run indefinitely. All writes use the caller's connection and existing wake guards;
this does not claim, spawn, relax budgets, or start another writer/thread.
"""
import time

from hermes_cli import loop_accounting

SERVICE_INTERVAL_SECONDS = 5.0


class WakeProtection:
    def __init__(self, conn, result, *, dry_run=False):
        self.conn = conn
        self.result = result
        self.dry_run = dry_run
        self.next_check = 0.0

    def checkpoint(self):
        if self.dry_run or self.conn.in_transaction or time.monotonic() < self.next_check:
            return
        from hermes_cli.kanban_continuation import wake_due_continuations
        woken = loop_accounting.timed(
            'dispatch.cooperative_wake', wake_due_continuations, self.conn)
        if woken:
            self.result.woken_continuations.extend(woken)
        # Rate-limit from completion, including an expensive scan's own cost.
        self.next_check = time.monotonic() + SERVICE_INTERVAL_SECONDS

    def timed(self, name, fn, *args, **kwargs):
        self.checkpoint()
        try:
            return loop_accounting.timed(name, fn, *args, **kwargs)
        finally:
            self.checkpoint()
