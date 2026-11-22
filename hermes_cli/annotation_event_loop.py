"""One event wait for projection work; lease maintenance never reads inputs."""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def run(events, stop, lease, refresh, *, interval=2.0):
    """refresh installs coverage before reading and returns an absolute deadline.

    A deadline is one-shot: refresh must consume it and return its successor or
    None. Heartbeats and standby takeover retain their existing lease cadence,
    but neither invokes refresh without a change, deadline, or new ownership.
    Errors schedule a bounded recovery attempt without certifying old inputs.
    """
    dirty, owned = True, False
    due = None
    beat_at = time.monotonic()
    while not stop.is_set():
        now = time.monotonic()
        if now >= beat_at:
            try:
                held = lease.held()
                if held:
                    lease.beat()
                if held and not owned:
                    events.control_only = False
                dirty |= held and not owned
                owned = held
            except Exception:  # health: allow BLE001 -- lease I/O failure must not retire the holder
                log.warning('annotation lease maintenance failed', exc_info=True)
                owned = False  # no source read until ownership is established again
                dirty = True
            beat_at = now + interval
        if owned and (dirty or (due is not None and time.time() >= due)):
            try:
                due = refresh()
                # An expired provider deadline must not create a busy spin.
                if due is not None and due <= time.time():
                    due = time.time() + interval
            except Exception:  # health: allow BLE001 -- preserve holder after a failed source read
                log.warning('annotation event refresh failed', exc_info=True)
                due = time.time() + interval
            dirty = False
        timeout = max(0.0, beat_at - time.monotonic())
        if owned and due is not None:
            timeout = min(timeout, max(0.0, due - time.time()))
        try:
            dirty |= events.wait(timeout)
        except OSError:
            log.warning('annotation event wait failed', exc_info=True)
            # Do not spin on a broken descriptor. The refresh reinstalls coverage.
            stop.wait(interval)
            events.control_only = False
            dirty = True
