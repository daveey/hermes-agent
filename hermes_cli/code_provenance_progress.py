"""Fixed-size private capture progress, not an authorization or completion receipt.

Only the owning worker samples CPU at phase boundaries. Elapsed/CPU values are
last samples, not timeout measurements; in-flight CPU remains unknown. No ledger
or custody locks, clock reads, callbacks or serialization under the recorder lock.
"""
from __future__ import annotations

import threading
import time

from hermes_cli.code_attestation import MAX_UNITS

INVENTORY_STAGES = frozenset(('unavailable', 'module_snapshot', 'module_metadata', 'module_file', 'finished'))
MAX_INVENTORY_UPDATES = 2 + 3 * MAX_UNITS

PHASES = frozenset(('worker_start', 'disk', 'ledger_lock', 'inventory', 'registry',
                    'projection', 'finalize', 'complete', 'unavailable'))
MAX_COUNTER_MS = 2**31 - 1
MAX_TRANSITIONS = 9


def unavailable():
    """Fresh fixed scalar fallback; never borrow another flight's observation."""
    return {'schema': 1, 'phase': 'unavailable', 'sampled_elapsed_ms': 0,
            'worker_cpu_ms': 0, 'cpu_is_lower_bound': True}


class CaptureProgress:
    __slots__ = ('_begin_ns', '_cpu_begin_ns', '_inventory', '_inventory_updates',
                 '_lock', '_sample', '_transitions')

    def __init__(self):
        self._begin_ns = time.monotonic_ns()
        self._cpu_begin_ns = None
        self._lock = threading.Lock()
        self._sample = unavailable()
        self._transitions = 0
        self._inventory = None
        self._inventory_updates = 0

    def set_phase(self, phase):
        if type(phase) is not str or phase not in PHASES:
            raise ValueError('invalid_capture_phase')
        elapsed_ns, cpu_ns = time.monotonic_ns(), time.thread_time_ns()
        with self._lock:
            if self._transitions >= MAX_TRANSITIONS:
                raise ValueError('capture_phase_limit')
            if self._cpu_begin_ns is None:
                self._cpu_begin_ns = cpu_ns
            self._transitions += 1
            self._sample = {'schema': 1, 'phase': phase,
                'sampled_elapsed_ms': min(MAX_COUNTER_MS, max(0, (elapsed_ns - self._begin_ns) // 1000000)),
                'worker_cpu_ms': min(MAX_COUNTER_MS, max(0, (cpu_ns - self._cpu_begin_ns) // 1000000)),
                'cpu_is_lower_bound': True}

    def set_inventory(self, stage, completed):
        if (type(stage) is not str or stage not in INVENTORY_STAGES or
                type(completed) is not int or not 0 <= completed <= MAX_UNITS):
            raise ValueError('invalid_inventory_progress')
        elapsed_ns, cpu_ns = time.monotonic_ns(), time.thread_time_ns()
        with self._lock:
            if (self._cpu_begin_ns is None or self._inventory_updates >= MAX_INVENTORY_UPDATES or
                    (self._inventory is not None and
                     (self._inventory['stage'] == 'finished' or completed < self._inventory['completed']))):
                raise ValueError('invalid_inventory_progress')
            self._inventory_updates += 1
            self._inventory = {'schema': 1, 'stage': stage, 'completed': completed,
                'sampled_elapsed_ms': min(MAX_COUNTER_MS, max(0, (elapsed_ns - self._begin_ns) // 1000000)),
                'worker_cpu_ms': min(MAX_COUNTER_MS, max(0, (cpu_ns - self._cpu_begin_ns) // 1000000)),
                'cpu_is_lower_bound': True}

    def snapshot(self):
        if not self._lock.acquire(blocking=False):
            return {**unavailable(), 'inventory': {'schema': 1, 'stage': 'unavailable', 'completed': 0,
                'sampled_elapsed_ms': 0, 'worker_cpu_ms': 0, 'cpu_is_lower_bound': True}}
        try:
            sample = dict(self._sample)
            if self._inventory is not None:
                sample['inventory'] = dict(self._inventory)
            return sample
        finally:
            self._lock.release()
