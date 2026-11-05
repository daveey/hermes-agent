"""Per-HTTP-attempt timing provenance stamped on a failed Board request's exception. [daveey]

Kept apart from ``kanban_sync_client`` (the HTTP client) and ``kanban_queue_reason`` (the reader that rounds and
records it), so neither imports the other's layer for it.
"""
import logging
from typing import Optional

ATTEMPT_ATTR = "board_attempt"


def note_attempt(exc: BaseException, deadline: Optional[float], started: float, ended: float) -> None:
    """Stamp the FAILED HTTP attempt's raw ``(budget_s, elapsed_s)`` on ``exc`` as optional provenance.

    ``budget_s`` is what the shared monotonic ``deadline`` had left when this attempt ``started`` (``None`` without
    a deadline) and ``elapsed_s`` is how long that attempt took to fail (``ended - started``). Only those two
    numbers: never the url, body or message. The innermost attempt wins (a redirect's own attempt is not
    overwritten by its caller), and stamping never replaces or masks the error being raised. Rounding/clamping for
    storage belongs to the reader (``kanban_queue_reason.attempt_timing``)."""
    try:
        if getattr(exc, ATTEMPT_ATTR, None) is None:
            budget = None if deadline is None else deadline - started
            setattr(exc, ATTEMPT_ATTR, (budget, ended - started))
    except Exception as err:  # health: allow BLE001 -- optional diagnostics must never mask the Board error
        logging.getLogger(__name__).debug("attempt timing not stamped (%s)", type(err).__name__)
