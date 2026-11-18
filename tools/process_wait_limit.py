"""How long one ``process_manage(action="wait")`` call may block. [daveey]

The wait used to be clamped to ``TERMINAL_TIMEOUT`` (180 s by default), which made it SHORTER than a
foreground ``sleep``: agents waiting on their own background job ran ``sleep 540; tail log`` instead
(measured on the hub over 7 days: 52 h in 2,838 sleep calls, half of them behind a tracked background
process). The wait returns the moment the process exits, so a longer window only removes turns.

The ceiling is the tool executor's per-call deadline (420 s by default): a call that outlives it is
abandoned and reported as a tool timeout, so the wait must end by itself first.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

DEFAULT_MAX_WAIT_SECONDS = 400
# Leave the executor room to collect the result before its own deadline fires.
_EXECUTOR_MARGIN_SECONDS = 20


def _executor_deadline_seconds() -> float | None:
    """The executor's per-call deadline when this process runs one; ``None`` when it does not (a
    direct registry caller) or the deadline is disabled. Read through ``sys.modules`` so a tool
    module never imports the agent loop."""
    executor = sys.modules.get("agent.tool_executor")
    resolve = getattr(executor, "_resolve_sequential_tool_timeout", None)
    if resolve is None:
        return None
    try:
        deadline = resolve()
    except Exception:  # a config read failure must not break a wait; the limit then stands alone
        logger.debug("executor deadline unavailable for the wait limit", exc_info=True)
        return None
    return deadline if deadline and deadline > 0 else None


def _positive_int(raw) -> int | None:
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    return value if value > 0 else None


def max_wait_seconds() -> int:
    """``TERMINAL_MAX_WAIT``, else the larger of 400 s and ``TERMINAL_TIMEOUT`` (an install that
    raised TERMINAL_TIMEOUT to get long waits keeps them), kept under the executor's per-call deadline."""
    limit = _positive_int(os.getenv("TERMINAL_MAX_WAIT"))
    if limit is None:
        limit = max(DEFAULT_MAX_WAIT_SECONDS, _positive_int(os.getenv("TERMINAL_TIMEOUT")) or 0)
    deadline = _executor_deadline_seconds()
    if deadline is not None:
        limit = min(limit, max(1, int(deadline) - _EXECUTOR_MARGIN_SECONDS))
    return limit
