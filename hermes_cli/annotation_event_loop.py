"""One event wait for projection work; lease maintenance never reads inputs."""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Minimum gap between reading refreshes. The first event after a quiet period is
# answered at once; later ones inside the gap share one trailing refresh, so a
# continuously written board costs a bounded number of projections per second.
COALESCE_S = 0.25


def run(events, stop, lease, refresh, *, interval=2.0, coalesce=COALESCE_S):
    """refresh installs coverage before reading and returns an absolute deadline.

    A deadline is one-shot: refresh must consume it and return its successor or
    None. Heartbeats and standby takeover retain their existing lease cadence,
    but neither invokes refresh without a change, deadline, or new ownership.
    Errors schedule a bounded recovery attempt without certifying old inputs.
    """
    dirty, owned = True, False
    source = True  # a pending wake that invalidates inputs (not demand-only)
    quiet = 0.0  # monotonic time before which no reading refresh may start
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
                    source = True
                dirty |= held and not owned
                owned = held
            except Exception:  # health: allow BLE001 -- lease I/O failure must not retire the holder
                log.warning('annotation lease maintenance failed', exc_info=True)
                owned = False  # no source read until ownership is established again
                dirty = True
            beat_at = now + interval
        expired = due is not None and time.time() >= due
        reads = source or expired
        deferred = owned and (dirty or expired) and reads and time.monotonic() < quiet
        if owned and (dirty or expired) and not deferred:
            events.control_only = not source
            try:
                due = refresh()
                # An expired provider deadline must not create a busy spin.
                if due is not None and due <= time.time():
                    due = time.time() + interval
            except Exception:  # health: allow BLE001 -- preserve holder after a failed source read
                log.warning('annotation event refresh failed', exc_info=True)
                due = time.time() + interval
            if reads:
                quiet = time.monotonic() + coalesce
            dirty = source = False
        timeout = max(0.0, beat_at - time.monotonic())
        if deferred:
            timeout = min(timeout, max(0.0, quiet - time.monotonic()))
        elif owned and due is not None:
            timeout = min(timeout, max(0.0, due - time.time()))
        try:
            changed = events.wait(timeout)
            dirty |= changed
            source |= changed and not getattr(events, 'control_only', False)
        except OSError:
            log.warning('annotation event wait failed', exc_info=True)
            # Do not spin on a broken descriptor. The refresh reinstalls coverage.
            stop.wait(interval)
            dirty = source = True
