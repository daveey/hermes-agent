"""Fresh module inventory preparation and bounded, payload-free scan timing.

Wall minus thread CPU does not identify GIL, filesystem or lock wait ownership.
Timing excludes lock acquisition and settlement, and includes off-lock preparation.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
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
                preparation = getattr(self, 'preparation_total', (0, 0))
                self.total = (monotonic_ns() - self.begin[0] + preparation[0],
                              thread_time_ns() - self.begin[1] + preparation[1])
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


def inventory_modules(ledger, *, deadline=None, diagnostics=None, prepared=None):
    from hermes_cli.code_attestation import MAX_UNITS, _check_inventory_deadline

    if diagnostics is not None:
        diagnostics.scanned = True
        diagnostics.count = 0
        if diagnostics.enabled:
            try:
                diagnostics.begin = monotonic_ns(), thread_time_ns()
            except Exception:  # health: allow BLE001 -- timing failure cannot stop the inventory
                diagnostics.enabled = False
    completed = False
    try:
        # Retain the initial observations too: a removed/replaced module or an
        # overwritten filename must not erase evidence seen during preparation.
        entries = [(name, item[0], item[1], item) for (name, _), item in (prepared or {}).items()]
        for name, module in ledger._module_snapshot():
            filename = getattr(module, '__file__', None)
            item = prepared.get((name, id(module))) if prepared is not None else None
            if item is None or item[0] is not module or item[1] != filename:
                entries.append((name, module, filename, None))
        for name, module, filename, item in entries:
            _check_inventory_deadline(deadline)
            if diagnostics is not None:
                diagnostics.count += 1
            if item is not None:
                if filename:
                    ledger._resolved_module_file(name, filename, item[2], diagnostics=diagnostics)
            else:
                # New/replaced modules and changed filenames never reuse a result.
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
    def __init__(self, ledger, *, deadline=None, enabled=True, prepare=False):
        self.ledger = ledger
        self.lock = ledger._lock
        self.deadline = deadline
        owned = getattr(self.lock, '_is_owned', None)
        outer = callable(owned) and not owned()
        self.diagnostics = InventoryDiagnostics() if enabled and outer else None
        self.prepare = prepare and outer
        self.prepared = None

    def __enter__(self):
        if self.prepare:
            try:
                self._prepare()
            except BaseException:
                if self.diagnostics is not None and getattr(self.diagnostics, 'scanned', False):
                    self.diagnostics.stop(False)
                    self.diagnostics.finish()
                raise
        self._acquire()
        return self.diagnostics

    def _acquire(self):
        # Keep the existing shared-deadline acquisition semantics.
        from time import monotonic

        acquired = (self.lock.acquire() if self.deadline is None else
                    self.lock.acquire(timeout=max(0.0, self.deadline - monotonic())))
        if not acquired:
            raise TimeoutError('code attestation inventory budget exhausted')

    def _prepare(self):
        from hermes_cli.code_attestation import _check_inventory_deadline

        # Only collect module identities under the ledger lock. Filesystem work
        # is fresh on EVERY call, never cached across observations or retries.
        self._acquire()
        try:
            if self.deadline is None:
                self.ledger._settle()
            else:
                self.ledger._settle(deadline=self.deadline)
            _check_inventory_deadline(self.deadline)
        except BaseException:
            self.lock.release()
            raise
        timer = self.diagnostics
        begin = None
        if timer is not None and timer.enabled:
            try:
                begin = monotonic_ns(), thread_time_ns()
                timer.begin = begin
                timer.scanned = True
            except Exception:  # health: allow BLE001 -- optional clocks cannot alter verification
                timer.enabled = False
        try:
            snapshot = tuple((name, module, getattr(module, '__file__', None))
                             for name, module in self.ledger._module_snapshot())
        finally:
            self.lock.release()
        prepared = {}
        for name, module, filename in snapshot:
            _check_inventory_deadline(self.deadline)
            if timer is not None:
                timer.count += 1
            path = None
            if filename and not filename.startswith('<'):
                path = (timer.call('resolve', Path(filename).resolve) if timer is not None
                        else Path(filename).resolve())
            prepared[(name, id(module))] = (module, filename, path)
        _check_inventory_deadline(self.deadline)
        self.prepared = prepared
        if begin is not None and timer is not None and timer.enabled:
            try:
                timer.preparation_total = monotonic_ns() - begin[0], thread_time_ns() - begin[1]
            except Exception:  # health: allow BLE001 -- optional clocks cannot alter verification
                timer.enabled = False

    def __exit__(self, _kind, _value, _traceback):
        self.lock.release()
        if self.diagnostics is not None and getattr(self.diagnostics, 'scanned', False):
            self.diagnostics.finish()
