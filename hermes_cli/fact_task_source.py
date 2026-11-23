"""Task content source for ChangeFeed; projection policy belongs to materializers. [daveey]

A source's namespaced identity and hydration callback are supplied by the host.
Hydration must select ONLY content covered by kanban_task_change. Operational
claims/leases/wake state require separate adapters; this is not a universal
board cursor. The callback receives batches of at most 500 task ids in a guarded
read snapshot and must return detached raw data, not perform external I/O.
"""
from pathlib import Path

from hermes_cli.fact_capture import CaptureBudgetExceeded, CaptureOverflow, CapturePending, capture_tasks
from hermes_cli.fact_bootstrap import PagedBootstrap
from hermes_cli.fact_source_watch import NativeSourceWatch


class TaskContentSource:
    def __init__(self, source, path, hydrate):
        self.source = source
        # Keep the lexical alias: native coverage must see symlink retargets.
        self.path = Path(path).absolute()
        self._hydrate = hydrate
        self._watch = None
        self._bootstrap = None

    def watch(self, wake):
        if self._watch is not None:
            raise RuntimeError("source already registered")
        self._watch = NativeSourceWatch((self.path,), wake)
        return self._watch

    def capture(self, cursor, budget):
        if self._watch is not None:
            self._watch.check()
        if (self._bootstrap is not None and self._bootstrap.ready is not None
                and cursor == self._bootstrap.ready.after):
            self._bootstrap = None
        if self._bootstrap is not None:
            result = self._bootstrap.capture(cursor, budget)
        else:
            try:
                result = capture_tasks(self.path, cursor, self._hydrate, budget=budget)
            except (CaptureOverflow, CaptureBudgetExceeded):
                self._bootstrap = PagedBootstrap(self.path, self._hydrate)
                raise CapturePending("task bootstrap required") from None
        if self._watch is not None:
            self._watch.check()
        return result
