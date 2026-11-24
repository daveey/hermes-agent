"""[daveey] Garbage-collector budget for long-lived gateway and serve processes.

A full (oldest generation) collection walks every tracked object while holding the GIL, so every
thread in the process stops for its whole length. Measured on a hub Desktop backend on 2026-11-23
(2.7 GB RSS, interpreter default thresholds 2000/10/10): one full collection took 1.6 s of CPU and
ran every 32 s (86 in 45 min), next to 7 young collections a second. Each one is a 1.6 s freeze of
the API loop and of every chat in the process.

Raising the young threshold makes every generation proportionally rarer: at 20,000 the same process
runs a full collection about every five minutes. The cost is memory: cyclic garbage waits ten times
as many allocations for its collection (about 60k objects per full pass at the old rate, so some
tens of MB in a multi-GB process). Reference-counted garbage, which is nearly all of it, is still
freed at once.

``HERMES_GATEWAY_GC_THRESHOLD0`` overrides the young threshold (0 or unparsable keeps the interpreter's).

Collections are timed from ``gc.callbacks`` and reported under ``gc`` in the loop-accounting
snapshot. The callback takes no lock and calls nothing that does: a collection can start inside
any allocation, including one made while the accounting lock is held.
"""
from __future__ import annotations

import gc
import logging
import os
import time

GATEWAY_GC_THRESHOLD0 = 20_000
ENV = "HERMES_GATEWAY_GC_THRESHOLD0"

log = logging.getLogger(__name__)

# One collection runs at a time (the collector is not re-entrant), so one slot is enough.
_started: list = [0.0, 0.0]  # thread CPU, wall
_totals: dict[int, list] = {}  # generation -> [runs, cpu_s, wall_s, longest wall_s, collected]


def gateway_gc_threshold0(environ=None) -> int | None:
    """Young-generation threshold to set, or None to keep the interpreter's."""
    raw = (environ if environ is not None else os.environ).get(ENV)
    if raw is None or not str(raw).strip():
        return GATEWAY_GC_THRESHOLD0
    try:
        value = int(float(raw))
    except (ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _on_gc(phase: str, info: dict) -> None:
    if phase == "start":
        _started[0], _started[1] = time.thread_time(), time.perf_counter()
        return
    cpu, wall = time.thread_time() - _started[0], time.perf_counter() - _started[1]
    row = _totals.get(info.get("generation", -1))
    if row is None:
        row = _totals.setdefault(info.get("generation", -1), [0, 0.0, 0.0, 0.0, 0])
    row[0] += 1
    row[1] += cpu
    row[2] += wall
    row[3] = max(row[3], wall)
    row[4] += info.get("collected", 0)


def apply_gateway_gc_budget(environ=None) -> tuple:
    """Set the process's collection thresholds and start timing collections; returns the thresholds in effect."""
    target = gateway_gc_threshold0(environ)
    if target is not None:
        _, middle, old = gc.get_threshold()
        try:
            gc.set_threshold(target, middle, old)
        except (ValueError, OverflowError) as exc:  # pragma: no cover - defensive
            log.warning("gateway gc threshold %r refused: %s", target, exc)
    if _on_gc not in gc.callbacks:
        gc.callbacks.append(_on_gc)
    return gc.get_threshold()


def snapshot() -> dict:
    """``{threshold, generations: {n: {runs, cpu_s, wall_s, max_wall_s, collected}}}`` since :func:`apply`."""
    return {
        "threshold": list(gc.get_threshold()),
        "timed": _on_gc in gc.callbacks,
        "generations": {str(gen): {"runs": row[0], "cpu_s": round(row[1], 4), "wall_s": round(row[2], 4),
                                   "max_wall_s": round(row[3], 4), "collected": row[4]}
                        for gen, row in sorted(_totals.items())},
    }
