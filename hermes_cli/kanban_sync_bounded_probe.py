"""Bounded Board transport helpers: the deadline request processor and per-send probes. [daveey]

``DeadlineRequests`` caps every urllib attempt (redirects included) at a Hub's remaining absolute budget.
``watch_written`` and ``schedstat`` feed the hub metrics sender's per-send INFO line (``machine_metrics_sender``):
whether any request bytes reached the socket, and Linux scheduler run/wait time of a thread.
"""
from __future__ import annotations

import urllib.request
from typing import Any, Callable, Optional


class DeadlineRequests(urllib.request.BaseHandler):
    """urllib invokes request processors again for each redirect, before sending."""

    def __init__(self, remaining: Callable[[float], float]) -> None:
        self.remaining = remaining

    def http_request(self, request):
        request.timeout = self.remaining(request.timeout)
        return request

    https_request = http_request


def watch_written(conn: Any, probe: dict) -> None:
    """Mark ``probe["written"] = True`` once the connection has handed request bytes to its socket."""
    probe.setdefault("written", False)
    send = conn.send

    def counted(data):
        send(data)
        probe["written"] = True
    conn.send = counted


def schedstat(tid: Optional[int] = None) -> Optional[tuple[int, int]]:
    """``(run_ns, wait_ns)`` of the calling thread (or native thread *tid* of this process) from
    ``/proc/.../schedstat``; None where it cannot be read (non-Linux, a thread that already exited)."""
    path = "/proc/thread-self/schedstat" if tid is None else f"/proc/self/task/{tid}/schedstat"
    try:
        with open(path, encoding="ascii") as fh:
            run_ns, wait_ns = fh.read().split()[:2]
        return int(run_ns), int(wait_ns)
    except (OSError, ValueError):
        return None
