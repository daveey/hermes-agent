"""Shared interpreter-shutdown predicate for background threads that can outlive
process teardown (cron delivery, concurrent tool submission, retry paths).

Once finalization starts, ``concurrent.futures`` refuses new work and asyncio's
default executor is gone, so any further scheduling only produces noise (stray
prints after the TUI exited, tracebacks in ``errors.log``, futile retries).
CPython raises ``cannot schedule new futures after interpreter shutdown``
(module-global flag) or ``... after shutdown`` (a pool whose ``shutdown()`` ran);
the short prefix catches both — safe here because every pool involved is a
module-global daemon or a ``with``-scoped local only finalization can shut down.
A first ``ThreadPoolExecutor`` use after the main thread returned imports
``concurrent.futures.thread``, whose ``threading._register_atexit`` raises
``can't register atexit after shutdown`` instead: the same exit, another message.
"""

from __future__ import annotations

import sys
from typing import Optional

_SHUTDOWN_MESSAGES = ("cannot schedule new futures", "can't register atexit after shutdown")


def interpreter_shutting_down(exc: Optional[BaseException] = None) -> bool:
    """True when the interpreter is finalizing. ``exc`` lets a caller treat an
    already-raised scheduling error as a shutdown signal: the ``concurrent.futures``
    flag can be set a hair before ``sys.is_finalizing()`` flips."""
    if sys.is_finalizing():
        return True
    if exc is None:
        return False
    text = str(exc).lower()
    return any(m in text for m in _SHUTDOWN_MESSAGES)
