"""Fixed-size private capture progress, not an authorization or completion receipt.

Only the owning worker samples CPU at phase boundaries. Elapsed/CPU values are
last samples, not timeout measurements; in-flight CPU remains unknown. No ledger
or custody locks, clock reads, callbacks or serialization under the recorder lock.
"""
from __future__ import annotations

import threading
import time

PHASES = frozenset(('worker_start', 'disk', 'ledger_lock', 'inventory', 'registry',
                    'projection', 'finalize', 'complete', 'unavailable'))
MAX_COUNTER_MS = 2**31 - 1
MAX_TRANSITIONS = 9


def unavailable():
    """Fresh fixed scalar fallback; never borrow another flight's observation."""
    return {'schema': 1, 'phase': 'unavailable', 'sampled_elapsed_ms': 0,
            'worker_cpu_ms': 0, 'cpu_is_lower_bound': True}


class CaptureProgress:
    __slots__ = ('_begin_ns', '_cpu_begin_ns', '_lock', '_sample', '_transitions')

    def __init__(self):
        self._begin_ns = time.monotonic_ns()
        self._cpu_begin_ns = None
        self._lock = threading.Lock()
        self._sample = unavailable()
        self._transitions = 0

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

    def snapshot(self):
        if not self._lock.acquire(blocking=False):
            return unavailable()
        try:
            return dict(self._sample)
        finally:
            self._lock.release()
