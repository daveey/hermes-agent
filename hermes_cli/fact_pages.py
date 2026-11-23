"""Term/scoped, expiring reset byte pages; not a new public fact schema. [daveey]"""
from __future__ import annotations

import threading
import time
import uuid
import tempfile
from dataclasses import dataclass

from hermes_cli.fact_wire import encode_into

PAGE_BYTES = 256 * 1024
MAX_RESET_BYTES = 64 * 1024 * 1024


@dataclass
class _Pin:
    scope: object
    stream: object
    size: int
    expires: float
    timer: threading.Timer


class PinnedPages:
    def __init__(self, *, ttl=30.0, max_bytes=MAX_RESET_BYTES, max_pins=8, directory=None):
        if ttl <= 0 or max_bytes < 1 or max_pins < 1:
            raise ValueError("positive reset limits required")
        self.ttl, self.max_bytes, self.max_pins = ttl, max_bytes, max_pins
        self._lock = threading.Lock()
        self._encode_lock = threading.Lock()
        self.directory = directory
        self._pins = {}
        self.retained_bytes = 0

    def pin(self, scope, delta):
        with self._encode_lock:
            with self._lock:
                available = self.max_bytes - self.retained_bytes
                if len(self._pins) >= self.max_pins:
                    raise ValueError("reset_budget_exceeded")
            stream = tempfile.TemporaryFile(dir=self.directory)
            try:
                size = encode_into(delta, stream, available)
                return self._install(scope, delta, stream, size)
            except (OSError, ValueError, TypeError):
                stream.close()
                raise

    def _install(self, scope, delta, stream, size):
        with self._lock:
            token = uuid.uuid4().hex
            timer = threading.Timer(self.ttl, self.release, args=(token, scope))
            timer.daemon = True
            self._pins[token] = _Pin(scope, stream, size, time.monotonic() + self.ttl, timer)
            self.retained_bytes += size
            timer.start()
            return {"token": token, "size": size, "epoch": delta.epoch, "rev": delta.rev}

    def page(self, token, scope, offset):
        with self._lock:
            pin = self._pins.get(token)
            if pin is None or pin.scope != scope or pin.expires <= time.monotonic():
                raise ValueError("reset_expired")
            if type(offset) is not int or offset < 0 or offset >= pin.size:
                raise ValueError("invalid_page_offset")
            pin.stream.seek(offset)
            return pin.stream.read(PAGE_BYTES)

    def release(self, token, scope):
        with self._lock:
            pin = self._pins.get(token)
            if pin is not None and pin.scope == scope:
                del self._pins[token]
                self.retained_bytes -= pin.size
                pin.timer.cancel()
                pin.stream.close()

    def close(self):
        with self._lock:
            for pin in self._pins.values():
                pin.timer.cancel()
                pin.stream.close()
            self._pins.clear()
            self.retained_bytes = 0
