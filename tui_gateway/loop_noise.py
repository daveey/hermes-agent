"""Suppress benign event-loop teardown noise on the gateway serving loop.

When the Desktop client forcibly closes its WebSocket while the gateway still has pending socket
operations, asyncio logs a traceback per pending ``_call_connection_lost`` callback —
``ConnectionResetError`` (WinError 10054), ``ConnectionAbortedError`` (10053) or ``BrokenPipeError``
(POSIX); one disconnect can emit 50+. They are the expected side effect of the peer hanging up
before our writes drained, so the handler here collapses exactly that class to one debug line and
forwards everything else to the previous handler unchanged. The same goes for a websocket closing
while the keepalive ping awaits its pong ("ConnectionClosedOK exception in shielded future").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

_log = logging.getLogger(__name__)

# Connection-teardown errors that mean "the peer hung up mid-write".
_BENIGN_TEARDOWN_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


def _is_benign_teardown(context: dict[str, Any]) -> bool:
    """True when the loop error is a peer-hangup during transport teardown.

    Gated on BOTH the exception type AND the ``_call_connection_lost`` callback (matched
    on repr) so the same error type raised elsewhere still reaches the default handler.
    """
    if not isinstance(context.get("exception"), _BENIGN_TEARDOWN_ERRORS):
        return False
    marker = "_call_connection_lost"
    return marker in repr(context.get("callback")) or marker in repr(context.get("handle"))


def _is_closed_websocket_in_shield(context: dict[str, Any]) -> bool:
    """True for asyncio's "<exc> exception in shielded future" when <exc> is a closed websocket.

    websockets' legacy keepalive awaits ``asyncio.shield(pong_waiter)``; on close the keepalive task
    is cancelled first and ``abort_pings`` then fails the waiter with ``ConnectionClosed`` — the
    normal end of a pinged connection (SSH-tunnel Desktop backends), not a loop error. Gated on the
    shield message so a ConnectionClosed escaping anywhere else still reaches the default handler.
    """
    if "exception in shielded future" not in str(context.get("message") or ""):
        return False
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        return False
    return isinstance(context.get("exception"), ConnectionClosed)


def install_loop_noise_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Chain a teardown-noise filter ahead of the loop's existing handler.

    Idempotent: a loop already carrying the filter is left alone, so it's safe to call
    on every reconnect/serve entry without stacking handlers.
    """
    if getattr(loop, "_hermes_noise_filter_installed", False):
        return
    previous = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _is_benign_teardown(context):
            _log.debug("ws peer hangup during teardown (suppressed): %s", context.get("exception"))
            return
        if _is_closed_websocket_in_shield(context):
            _log.debug("ws closed while keepalive awaited a pong (suppressed): %s", context.get("exception"))
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    with contextlib.suppress(AttributeError, TypeError):  # pragma: no cover - exotic loop impls
        loop._hermes_noise_filter_installed = True  # type: ignore[attr-defined]
