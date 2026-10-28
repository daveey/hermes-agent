"""The logger every Hermes websockets connection passes as ``logger=``.

websockets' keepalive thread/task reports a peer that stopped answering pings with
``logger.error("keepalive ping failed", exc_info=True)``. By then the library has already failed the
connection, and the caller's next ``recv``/``send`` raises ``ConnectionClosed``, which every Hermes call
site handles (reconnect, end the stream, fall back to polling). Logged at ERROR with a traceback, that
handled drop reads in errors.log like an unhandled crash, and self-heal files it as a defect.

``ws_logger()`` wraps the library's own logger (same name, so level and handler config still apply),
turns that one message into a single INFO line naming the cause, and passes every other record through at
its real level. ``scripts/code_health`` rule HX015 flags a websockets ``connect()`` without ``logger=``,
so a new call site cannot reintroduce the ERROR.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_HANDLED = frozenset({"keepalive ping failed"})


class _HandledDropAdapter(logging.LoggerAdapter):
    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        if level >= logging.ERROR and isinstance(msg, str) and msg in _HANDLED:
            exc = sys.exc_info()[1]
            kwargs.pop("exc_info", None)
            level, msg, args = logging.INFO, "%s (%s: %s); the connection is closed and the caller handles it", (
                msg, type(exc).__name__ if exc else "no exception", exc)
        super().log(level, msg, *args, **kwargs)


def ws_logger(side: str = "client") -> logging.LoggerAdapter:
    """Logger for one websockets connection (``connect(..., logger=ws_logger())``; ``serve(..., logger=
    ws_logger("server"))``)."""
    return _HandledDropAdapter(logging.getLogger(f"websockets.{side}"), {})
