"""Overhead pin for loop accounting: one pass must cost <= 20 µs (design t_01a1d563324d7e8e §2 P1). [daveey]"""

from __future__ import annotations

import time

from hermes_cli import loop_accounting as la

BUDGET_S = 20e-6


def _per_pass(n: int) -> float:
    best = float("inf")
    for _ in range(5):  # best of 5: a loaded CI host must not fail a cheap path
        t0 = time.perf_counter()
        for _ in range(n):
            with la.pass_("bench") as p:
                p.rows(1)
        best = min(best, (time.perf_counter() - t0) / n)
    return best


def test_pass_overhead_under_budget():
    la.reset()
    try:
        per = _per_pass(20000)
        assert per <= BUDGET_S, f"{per * 1e6:.2f} µs per pass"
    finally:
        la.reset()


def test_decorator_overhead_under_budget():
    la.reset()

    @la.accounted("bench.dec")
    def noop() -> None:
        return None

    try:
        best = float("inf")
        for _ in range(5):
            t0 = time.perf_counter()
            for _ in range(20000):
                noop()
            best = min(best, (time.perf_counter() - t0) / 20000)
        assert best <= BUDGET_S, f"{best * 1e6:.2f} µs per call"
    finally:
        la.reset()
