"""Bounded, payload-free requested-module-inventory timing; never an admission input.

Wall minus thread CPU does not identify GIL, filesystem or lock wait ownership.
The outer caller's lock acquisition and pending-code settlement are not measured.
"""
from __future__ import annotations

import os
import sys
import threading
from time import monotonic_ns, thread_time_ns

# Never import logging from an audited inventory: its first import may execute
# stdlib generators after their provenance boundary. Gate optional timing on the
# service's already-initialized sink instead of changing the observed import graph.
_logging = sys.modules.get('logging')
logger = _logging.getLogger(__name__) if _logging is not None else None


class InventoryDiagnostics:
    def __init__(self):
        global logger
        self.stages = {name: [0, 0, 0] for name in ('resolve', 'owner', 'classify')}
        self.count = 0
        self.enabled = False
        self.sink = logger
        acquired = False
        setup_lock = None
        try:
            existing = sys.modules.get('logging')
            setup_lock = getattr(existing, '_lock', None)
            # getLogger and an isEnabledFor cache miss both take this RLock.
            # Optional observation must not queue behind logging configuration;
            # declining timing never skips the underlying attestation inventory.
            if setup_lock is not None:
                acquired = setup_lock.acquire(blocking=False)
                if not acquired:
                    return
            if self.sink is None:
                getter = getattr(existing, 'getLogger', None)
                self.sink = getter(__name__) if callable(getter) else None
                logger = self.sink
            self.enabled = self.sink is not None and self.sink.isEnabledFor(20)
            self.begin = (monotonic_ns(), thread_time_ns()) if self.enabled else None
        except Exception:  # health: allow BLE001 -- optional observer failures must not replace an inventory result
            self.enabled = False
        finally:
            if acquired and setup_lock is not None:
                setup_lock.release()

    def call(self, stage, function, *args):
        begin = None
        if self.enabled:
            try:
                begin = monotonic_ns(), thread_time_ns()
            except Exception:  # health: allow BLE001 -- a failed clock disables diagnostics, not the observed operation
                self.enabled = False
        try:
            return function(*args)
        finally:
            if begin is not None and self.enabled:
                try:
                    row = self.stages[stage]
                    row[0] += 1
                    row[1] += monotonic_ns() - begin[0]
                    row[2] += thread_time_ns() - begin[1]
                except Exception:  # health: allow BLE001 -- preserve the operation's exact exception/result
                    self.enabled = False

    def stop(self, completed):
        self.outcome = 'completed' if completed else 'aborted'
        self.total = None
        if self.enabled:
            try:
                self.total = monotonic_ns() - self.begin[0], thread_time_ns() - self.begin[1]
            except Exception:  # health: allow BLE001 -- optional clocks cannot replace inventory failure
                self.enabled = False

    def finish(self):
        if not self.enabled:
            return
        try:
            total = getattr(self, 'total', None)
            if total is None:
                total = monotonic_ns() - self.begin[0], thread_time_ns() - self.begin[1]
            stages = {key: (row[0], row[1] / 1_000_000, row[2] / 1_000_000)
                      for key, row in self.stages.items()}
            stages['modules'] = (self.count,
                max(0, total[0] - sum(r[1] for r in self.stages.values())) / 1_000_000,
                max(0, total[1] - sum(r[2] for r in self.stages.values())) / 1_000_000)
            self.sink.info('code_inventory pid=%d thread=%d request_ns=%d modules=%d '
                        'wall_ms=%.3f cpu_ms=%.3f outcome=%s stages=%s', os.getpid(),
                        threading.get_ident(), self.begin[0], self.count,
                        total[0] / 1_000_000, total[1] / 1_000_000,
                        getattr(self, 'outcome', 'completed'), stages)
        except Exception:  # health: allow BLE001 S110 -- an optional sink/clock failure must never replace an inventory result; no recursive logging
            pass


def inventory_modules(ledger, *, deadline=None, diagnostics=None):
    from hermes_cli.code_attestation import MAX_UNITS, _check_inventory_deadline

    if diagnostics is not None:
        diagnostics.scanned = True
        if diagnostics.enabled:
            try:
                diagnostics.begin = monotonic_ns(), thread_time_ns()
            except Exception:  # health: allow BLE001 -- timing failure cannot stop the inventory
                diagnostics.enabled = False
    completed = False
    try:
        # Recheck every mutable filename and freshly resolve every path, including
        # unchanged module identities. This is timing, not a cache or trust shortcut.
        for name, module in ledger._module_snapshot():
            _check_inventory_deadline(deadline)
            if diagnostics is not None:
                diagnostics.count += 1
            filename = getattr(module, '__file__', None)
            ledger._module_file(name, filename, diagnostics=diagnostics)
            key = (name, id(module))
            identity = (module, filename)
            if ledger._modules.get(key) == identity:
                continue
            if len(ledger._modules) >= MAX_UNITS:
                ledger.mark_unknown('module-inventory-limit')
                continue
            ledger._modules[key] = identity
            ledger._generation += 1
        completed = True
    finally:
        if diagnostics is not None:
            diagnostics.stop(completed)


class InventoryObservation:
    """Only the outer ledger reader may look up or emit to a logging sink.

    Nested/private inventories remain fully verified but uninstrumented. Timing
    starts inside the scan and stops before release; logging runs only off-lock.
    """
    def __init__(self, ledger, *, deadline=None, enabled=True):
        self.lock = ledger._lock
        self.deadline = deadline
        owned = getattr(self.lock, '_is_owned', None)
        self.diagnostics = InventoryDiagnostics() if enabled and callable(owned) and not owned() else None

    def __enter__(self):
        # Keep the existing shared-deadline acquisition semantics.
        from time import monotonic

        acquired = (self.lock.acquire() if self.deadline is None else
                    self.lock.acquire(timeout=max(0.0, self.deadline - monotonic())))
        if not acquired:
            raise TimeoutError('code attestation inventory budget exhausted')
        return self.diagnostics

    def __exit__(self, _kind, _value, _traceback):
        self.lock.release()
        if self.diagnostics is not None and getattr(self.diagnostics, 'scanned', False):
            self.diagnostics.finish()
