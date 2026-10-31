"""The logger every Hermes websockets connection passes as ``logger=``.

websockets logs some failures it has already turned into a closed connection at ERROR with a traceback.
The caller sees them as an exception every Hermes call site handles (reconnect, end the stream, fall back to
polling), but in errors.log they read like an unhandled crash, and self-heal files them as defects:

- ``"keepalive ping failed"``: the peer stopped answering pings; the library fails the connection and the
  caller's next ``recv``/``send`` raises ``ConnectionClosed``.
- ``"unexpected internal error"`` with ``EOFError("stream ended")``: ``connect()`` gave up on the opening
  handshake (``open_timeout``, typical on a loaded host) and closed the stream while its reader thread was
  still feeding the handshake response into it. ``connect()`` has already raised ``TimeoutError`` to the
  caller. This race is inside websockets (reproduced on 15.0.1 and 17.4 with no Hermes code). Any other
  exception under that message is a real internal error and keeps ERROR and its traceback.

``ws_logger()`` wraps the library's own logger (same name, so level and handler config still apply), turns
exactly those (message, exception) pairs into a single INFO line naming the cause, and passes every other
record through at its real level. ``scripts/code_health`` rule HX015 flags a websockets ``connect()`` without
``logger=``, so a new call site cannot reintroduce the ERROR.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional


def _any_exception(exc: Optional[BaseException]) -> bool:
    return True


def _closed_stream_eof(exc: Optional[BaseException]) -> bool:
    return type(exc) is EOFError and exc.args == ("stream ended",)


# message -> predicate on the exception being handled; a record is demoted only when both match.
_HANDLED: dict[str, Callable[[Optional[BaseException]], bool]] = {
    "keepalive ping failed": _any_exception,
    "unexpected internal error": _closed_stream_eof,
}


class _HandledDropAdapter(logging.LoggerAdapter):
    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        if level >= logging.ERROR and isinstance(msg, str) and msg in _HANDLED:
            exc = sys.exc_info()[1]
            if _HANDLED[msg](exc):
                kwargs.pop("exc_info", None)
                level, msg, args = logging.INFO, "%s (%s: %s); the connection is closed and the caller handles it", (
                    msg, type(exc).__name__ if exc else "no exception", exc)
        super().log(level, msg, *args, **kwargs)


def ws_logger(side: str = "client") -> logging.LoggerAdapter:
    """Logger for one websockets connection (``connect(..., logger=ws_logger())``; ``serve(..., logger=
    ws_logger("server"))``)."""
    return _HandledDropAdapter(logging.getLogger(f"websockets.{side}"), {})
