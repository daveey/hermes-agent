"""Conservative persistent-work observations, independent of CPU admission.

A populated worker scope is positive evidence of occupied work (a lower bound).
An empty pool is NOT a complete census: unscoped jobs, app.slice descendants and
in-process baseline work are not accounted for. Until that census is available,
this producer deliberately never emits zero. Missing means UNKNOWN, not idle.
No process registry is opened and no service is stopped to make an observation.
"""
from __future__ import annotations

import sys
import time

FIELDS = ('persistent_jobs', 'persistent_jobs_at_ms')
MAX_AGE_MS = 2000


def collect() -> dict:
    """Stamp before reading; a slow census must not rejuvenate old evidence."""
    from hermes_cli import worker_compute_capacity as capacity
    if not sys.platform.startswith('linux'):
        return {}
    at_ms = int(time.time() * 1000)
    try:
        occupied = capacity.occupied_owners(capacity.pool_path(), {}, set())
    except (OSError, ValueError):
        return {}
    if not occupied:
        return {}  # baseline/unscoped coverage remains UNKNOWN even after a job exits
    return fresh({'persistent_jobs': len(occupied), 'persistent_jobs_at_ms': at_ms})


def fresh(sample: dict) -> dict:
    """Drop incomplete, invalid or aged pairs at the send boundary; never re-stamp."""
    count, at_ms = (sample.get(key) for key in FIELDS)
    if (type(count) is int and 0 <= count <= 100000 and type(at_ms) is int
            and at_ms > 0 and 0 <= time.time() * 1000 - at_ms <= MAX_AGE_MS):
        return sample
    return {key: value for key, value in sample.items() if key not in FIELDS}
