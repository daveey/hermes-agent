"""Payload-free, per-call phase timing for the lease-free quiet check only.

Wall time includes waiting inside a phase; thread CPU does not. Neither value
identifies a lock owner or attributes concurrent work to this request.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import threading
from time import monotonic_ns, thread_time_ns

logger = logging.getLogger(__name__)


class CheckDiagnostics:
    def __init__(self):
        self.request_ns = monotonic_ns()
        self.pid = os.getpid()
        self.thread = threading.get_ident()

    @contextmanager
    def phase(self, name):
        wall = monotonic_ns()
        cpu = thread_time_ns()
        try:
            yield
        finally:
            try:
                wall_ms = (monotonic_ns() - wall) / 1_000_000
                cpu_ms = (thread_time_ns() - cpu) / 1_000_000
                logger.info(
                    "quiet_check_phase pid=%d thread=%d request_ns=%d phase=%s wall_ms=%.3f cpu_ms=%.3f",
                    self.pid, self.thread, self.request_ns, name, wall_ms, cpu_ms)
            except Exception:  # health: allow BLE001 S110 -- a failed optional log sink must not replace a refusal/result/read exception; logging that failure would reenter the same sink
                pass
