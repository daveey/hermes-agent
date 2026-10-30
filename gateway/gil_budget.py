"""[daveey] Interpreter switch interval for the gateway process.

The gateway is one Python process. Its asyncio loop serves the whole local API (Desktop conn=local, gateway
tickets), and the same process runs every agent conversation loop, AnyIO worker threads for sync routes, and
plugin thread pools. Each await the loop resumes needs the GIL back. When CPU-bound threads are runnable, a waiter
gets it only after the holder's switch interval expires, which is 5 ms by default, and often only after other
runnable threads have each taken a turn (the convoy effect). One API request crosses many such handoffs.

Measured on David-MBP on 2026-10-30, under load avg 38: an asyncio echo server answering beside K pure-Python
burner threads.

    K=1  5 ms: p50  222 ms     1 ms: p50  36 ms
    K=4  5 ms: p50  750 ms     1 ms: p50 148 ms (p95 1.6 s -> 0.24 s)

During 11:08-11:27 PDT that day (load 66-96), the gateway loop thread spent 64% of its samples in take_gil, and
666 Desktop reads on conn=local missed their 4 s timeout. hub-sync/status, which costs 2 ms of CPU, ran up to
5.45 s. A 1 ms interval slices the CPU-bound threads finely enough that the loop thread stays responsive. The cost
is a few more GIL handoffs per second for those threads. mcp_startup's discovery window already drops to 0.5 ms
for the same reason; it restores whatever is set here.

``HERMES_GATEWAY_SWITCH_INTERVAL_MS`` overrides the interval (0 or unparsable keeps the interpreter default).
"""
from __future__ import annotations

import logging
import os
import sys

GATEWAY_SWITCH_INTERVAL_S = 0.001
ENV = "HERMES_GATEWAY_SWITCH_INTERVAL_MS"

log = logging.getLogger(__name__)


def gateway_switch_interval(environ=None) -> float | None:
    """Seconds to set, or None to keep the interpreter default."""
    raw = (environ if environ is not None else os.environ).get(ENV)
    if raw is None or not str(raw).strip():
        return GATEWAY_SWITCH_INTERVAL_S
    try:
        ms = float(raw)
    except ValueError:
        return None
    return ms / 1000.0 if ms > 0 else None


def apply_gateway_switch_interval(environ=None) -> float:
    """Set the process switch interval for a gateway; returns the interval now in effect."""
    target = gateway_switch_interval(environ)
    if target is not None:
        try:
            sys.setswitchinterval(target)
        except (ValueError, OverflowError) as exc:  # pragma: no cover - defensive
            log.warning("gateway switch interval %r refused: %s", target, exc)
    return sys.getswitchinterval()
