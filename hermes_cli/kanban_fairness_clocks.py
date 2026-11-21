"""Writer-side clock samples for explicitly armed fairness journals.

These are local observation clocks, not the remote lifecycle event's clocks.
The UTC uncertainty is declared by the operator at arm time, not inferred from
export time. Registering the function does no I/O and observes nothing until SQL
calls it inside a clock-enabled capture transaction.
"""
from __future__ import annotations

import json
import time


def sample(clock_error_ns):
    before = time.monotonic_ns()
    wall = time.time_ns()
    after = time.monotonic_ns()
    return json.dumps({"wall_ns": wall, "mono_ns": before,
                       "clock_error_ns": clock_error_ns + after - before}, separators=(",", ":"))


def register(conn):
    conn.create_function("hermes_fairness_clock", 1, sample)
