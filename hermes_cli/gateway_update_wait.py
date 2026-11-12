"""[daveey] A finite run (``hermes -z``, ``chat -q``) started while a core update holds the gateway waits for
the restarted owner instead of failing.

During an update's swap the owner admits nothing new: ``ensure_gateway_runtime`` answers ``draining``
(``update_paused`` while the update fence is up, ``restart_wait`` inside a restart's after-turn wait) or
``starting``, and an attached owner that is draining refuses ``session.create`` / ``prompt.submit`` with
``runtime_draining`` (``runtime_retired`` once its authority retired). Each of these refusals means nothing
was admitted, so asking the next owner again is safe. A finite run used to exit 1 on the first one
(``Error: Gateway draining: update_paused``) and lose its prompt; selfheal probes and scripts that run
``hermes -z`` on a timer hit it on every hub apply. ``UpdateWait`` retries them for at most
``ADMISSION_WAIT_S`` from the first refusal (the same 10 minutes a running one-shot waits for its restarted
owner, ``GatewayChatView._redial``), with one stderr notice. Any other refusal, and every failure while not
yet waiting, keeps the old exit.
"""
import asyncio
import logging
import sys

from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, GatewayUnavailableError

logger = logging.getLogger(__name__)

ADMISSION_WAIT_S = 600.0
ADMISSION_RETRY_S = 2.0
# ``ensure_gateway_runtime`` verdicts of an owner that is going away or coming up (never ``inaccessible``,
# ``incompatible`` or a failed start, which stay final).
_WAIT_STATES = frozenset({"draining", "starting"})
# In-band refusals of an owner that admits nothing new while it drains (or whose authority retired).
_WAIT_RPC_REASONS = frozenset({"runtime_draining", "runtime_retired"})


def refused_for_restart(exc) -> bool:
    """True when *exc* is an owner refusing new work only because it is draining, retiring or starting."""
    if isinstance(exc, GatewayUnavailableError):
        return exc.state in _WAIT_STATES
    if isinstance(exc, GatewayRPCError):
        return str(exc) in _WAIT_RPC_REASONS
    return False


def _dial_failure(exc) -> bool:
    """A refused, timed-out or half-started dial (websockets' handshake rejections pass through raw)."""
    from websockets.exceptions import InvalidHandshake
    return isinstance(exc, (OSError, TimeoutError, InvalidHandshake))


class UpdateWait:
    """One finite run's bounded pre-admission wait; its window opens at the first refusal and is shared
    by every later refusal of the same run (attach and submit), so the whole wait stays bounded."""

    def __init__(self, *, announce=True):
        self.announce = announce  # False for stream-json: stderr notices are not part of that protocol
        self.deadline = None
        self.retries = 0

    @property
    def waiting(self) -> bool:
        return self.deadline is not None

    async def retry(self, exc):
        """Sleep before the next attempt, or raise *exc*: it is not a restart refusal (a dial failure
        counts once waiting), or the window has passed."""
        if not (refused_for_restart(exc) or (self.waiting and _dial_failure(exc))):
            raise exc
        loop = asyncio.get_running_loop()
        if self.deadline is None:
            self.deadline = loop.time() + ADMISSION_WAIT_S
            if self.announce:
                print(f"Gateway is restarting for an update ({exc}); waiting up to {ADMISSION_WAIT_S / 60:.0f} "
                      "min for the restarted gateway to take this prompt.", file=sys.stderr, flush=True)
        elif loop.time() >= self.deadline:
            logger.debug("gateway admission wait gave up: %s", exc)
            raise exc
        self.retries += 1
        await asyncio.sleep(ADMISSION_RETRY_S)

    async def dial(self, connect):
        """``await connect()`` until an owner takes the connection, retrying restart refusals."""
        from websockets.exceptions import InvalidHandshake
        while True:
            try:
                return await connect()
            except (GatewayClientError, OSError, TimeoutError, InvalidHandshake) as exc:
                await self.retry(exc)
