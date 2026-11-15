"""[daveey] Post-wake grace for lapsed session turn leases.

A turn lease row expires on the wall clock, but its holder renews on the awake clock (the shared
scheduler waits on ``time.monotonic``, which stops while macOS sleeps). Across a host suspend longer
than the TTL every live holder's row is expired at the instant the host wakes, and whichever process
runs first wins: a waiter polling every second beats the holder's 60 s refresher tick, takes the row,
and the holder's finished reply is refused ("another Hermes process took over this session").

``recently_woke()`` says whether this host suspended within the last ``WAKE_GRACE_SECONDS`` of awake
time. Acquisition then treats an expired row whose holder is not provably dead as still held, so the
holder's post-wake refresher (``DurableTurnLease._renew_after_lapse``) gets its tick. A holder that
never renews is reclaimed once the grace has passed; a dead holder is reclaimed at once.

Suspend detection: the suspend-inclusive clock (``hermes_platform.quiet_clock``) minus the awake
clock grows by exactly the suspended time, host-wide. The tracker keeps the last sample per process
and records the awake instant at which it first saw growth. Darwin also supplies a native host-wide
wake instant, so a process born after resume honours the remaining grace without a pre-sleep baseline.
Without either signal there is no deferral.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from hermes_platform.wake_clock import host_wake_awake_s

# Longer than the refresher's default 60 s interval plus its write patience, so one post-wake tick lands.
WAKE_GRACE_SECONDS = 90.0
# Clock-read jitter between the two clocks is microseconds; growth past this is a suspend.
_SUSPEND_STEP_SECONDS = 5.0

_lock = threading.Lock()
_last_total: Optional[float] = None
_woke_at: Optional[float] = None


def suspended_total_s() -> Optional[float]:
    """Seconds this host has spent suspended since boot, or ``None`` without a suspend-inclusive clock."""
    try:
        from hermes_platform.quiet_clock import suspend_inclusive_ns
    except ImportError:
        return None
    inclusive = suspend_inclusive_ns()  # None on hosts without a suspend-inclusive clock; never raises
    if inclusive is None:
        return None
    return (inclusive - time.monotonic_ns()) / 1e9


def _awake_now() -> float:
    return time.monotonic()


def recently_woke() -> bool:
    """True within the host's native wake grace or this process's observed suspend grace."""
    global _last_total, _woke_at
    total = suspended_total_s()
    host_woke_at = host_wake_awake_s()
    now = _awake_now()
    with _lock:
        if total is not None:
            if _last_total is not None and total - _last_total > _SUSPEND_STEP_SECONDS:
                _woke_at = now
            _last_total = total if _last_total is None else max(_last_total, total)
        woke_at = _woke_at
    return ((host_woke_at is not None and 0 <= now - host_woke_at < WAKE_GRACE_SECONDS)
            or (woke_at is not None and 0 <= now - woke_at < WAKE_GRACE_SECONDS))


def lapsed_row_reclaimable(holder: str, expires_at: float, now: float) -> bool:
    """Expired-row half of the turn-lease stale rule: expired, and not inside a post-wake grace."""
    if float(expires_at) > now:
        return False
    return not recently_woke()


def stale_turn_lease(row, now: float) -> bool:
    """A turn-lease row a lease-less transcript writer may delete (``_check_transcript_write_guards``): a
    provably dead holder, or the acquisition expiry rule above, so a wake does not let the first detached
    delivery delete a live holder's lapsed row before its post-wake renewal."""
    from hermes_state import _compression_lock_holder_process_is_dead
    return (_compression_lock_holder_process_is_dead(row["holder"])
            or lapsed_row_reclaimable(row["holder"], row["expires_at"], now))


def _reset_for_tests() -> None:
    global _last_total, _woke_at
    with _lock:
        _last_total = None
        _woke_at = None


# Baseline at import, so the first acquisition after a sleep in a long-running process sees the growth.
recently_woke()
