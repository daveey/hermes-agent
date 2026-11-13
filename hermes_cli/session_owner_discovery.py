"""[daveey] Bounded owner discovery for a session write from a backend not composed with the gateway.

One ``identify`` that misses its deadline does not prove the profile owner-less: the gateway's
control loop can stall for about a second under host load (hub, Nov 11: loop blocked 1084.8 ms,
an identify answered in 692.6 ms) and a single 1.5 s read then answers ``control_timeout``.
Every session write behind it was refused with the offline lock's 503, although the owner was
live and answered the next read.

A ``control_timeout`` is therefore re-read, read-only and before any mutation is sent, inside
one monotonic budget.

``starting`` is the other verdict that only needs time. A gateway restart holds the profile's
lock from its first moment but publishes READY only once adapters, cron and housekeeping run
(hub, 97 starts over 36 h: control socket after 8.3 s median, READY after 22.1 s median, 25.2 s
p90). A write in that window lost the offline lock and was refused with "Drain and stop the
gateway" although the gateway only had to finish starting (hub, Nov 13: archives 4 s and 10 s
after a restart). ``starting`` is re-read on its own longer budget, which also covers a missed
identify deadline while the starting gateway's loop is busy.

Every other verdict is final on its first read: ``absent`` keeps the offline path, and refusals
about authorization, protocol, permissions or profile identity are never retried. Discovery never
starts a gateway, and the mutation itself is never retried here: the caller sends it at most
once, and a client retry replays its own request_id.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

# Total wall-clock spent discovering one write's owner, from the request's point of view.
OWNER_DISCOVERY_BUDGET_S = 6.0
# Per-read socket deadline: the single read this replaces.
OWNER_DISCOVERY_ATTEMPT_S = 1.5
OWNER_DISCOVERY_ATTEMPTS = 3
OWNER_DISCOVERY_BACKOFF_S = 0.25
# A read with less time than this left is not started (discovery refuses a timeout <= 0).
_MIN_ATTEMPT_S = 0.1

# One write's whole wait for a gateway seen ``starting``, measured from the first read. Desktop
# cuts a request at 30 s (DEFAULT_FETCH_TIMEOUT_MS), so this leaves the forward its own seconds.
OWNER_STARTING_BUDGET_S = 24.0
# A ``starting`` read answers at once (lock probe or identify), so it is polled, not backed off.
OWNER_STARTING_POLL_S = 0.5

CONTROL_TIMEOUT = "control_timeout"
# The exhausted-retry 503 tail. The offline lock's own remedy ("Drain and stop the gateway") is
# wrong here: the owner is live, nothing was written, and the edit only has to be sent again.
CONTROL_TIMEOUT_DETAIL = ("owner_unavailable: control_timeout: the gateway that owns this profile "
                          "did not answer in time; nothing was changed, retry")
# The same for a gateway still starting when its budget ran out: it holds the lock because it
# is coming up, so telling the user to drain and stop it is wrong too.
STARTING_DETAIL = ("owner_unavailable: starting: the gateway that owns this profile is starting; "
                   "nothing was changed, retry")


def is_starting(discovery) -> bool:
    """A gateway that holds the profile but has not published READY: no endpoint yet."""
    return getattr(discovery, "endpoint", None) is None and getattr(discovery, "state", None) == "starting"


def is_transient(discovery) -> bool:
    """Only a missed identify deadline with no endpoint is worth another read."""
    return (getattr(discovery, "endpoint", None) is None and getattr(discovery, "state", None) == "inaccessible"
            and getattr(discovery, "reason_code", None) == CONTROL_TIMEOUT)


def owner_unavailable_detail(discovery, lock_detail) -> str:
    """The 503 detail once the offline lock lost behind a non-``absent`` verdict."""
    if is_transient(discovery):
        return CONTROL_TIMEOUT_DETAIL
    if is_starting(discovery):
        return STARTING_DETAIL
    reason = discovery.reason_code or discovery.state
    return f"owner_unavailable: {reason}: {lock_detail}"


def _timed_out():
    from hermes_cli.gateway_runtime import GatewayDiscovery
    return GatewayDiscovery("inaccessible", reason_code=CONTROL_TIMEOUT)


async def discover_session_owner(home: Path, *, clock=time.monotonic, sleep=asyncio.sleep):
    """Discovery's verdict for *home*, re-reading ``control_timeout`` and ``starting`` in budget.

    Each read runs in a thread under ``asyncio.wait_for`` capped at the remaining budget, so a
    read that overruns its own socket deadline (the multiplexer fallback can take two) cannot
    stretch the write past its budget; an abandoned read ends on its own socket timeout and is
    treated as another ``control_timeout``.

    Until a read says ``starting`` the budget is ``OWNER_DISCOVERY_BUDGET_S`` and at most
    ``OWNER_DISCOVERY_ATTEMPTS`` reads. The first ``starting`` moves the deadline to
    ``OWNER_STARTING_BUDGET_S`` after the first read began and lifts the read cap: from then on
    both transient verdicts are re-read until another verdict or that deadline."""
    from hermes_cli import gateway_runtime

    began = clock()
    deadline = began + OWNER_DISCOVERY_BUDGET_S
    discovery, starting, attempt = None, False, 0
    while starting or attempt < OWNER_DISCOVERY_ATTEMPTS:
        pause = OWNER_STARTING_POLL_S if is_starting(discovery) else OWNER_DISCOVERY_BACKOFF_S
        remaining = deadline - clock()
        if attempt and remaining < pause + _MIN_ATTEMPT_S:
            break
        if attempt:
            await sleep(pause)
            remaining = deadline - clock()
        attempt += 1
        read = asyncio.to_thread(gateway_runtime.discover_gateway_endpoint, home,
                                 timeout=min(OWNER_DISCOVERY_ATTEMPT_S, remaining))
        try:
            discovery = await asyncio.wait_for(read, timeout=remaining)
        except TimeoutError:
            discovery = _timed_out()
        if is_starting(discovery):
            if not starting:
                starting, deadline = True, began + OWNER_STARTING_BUDGET_S
        elif not is_transient(discovery):
            return discovery
    return discovery or _timed_out()  # the first read always runs; this only types the result
