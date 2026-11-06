"""Per-HTTP-attempt timing provenance stamped on a failed Board request's exception. [daveey]

Kept apart from ``kanban_sync_client`` (the HTTP client) and ``kanban_queue_reason`` (the reader that rounds and
records it), so neither imports the other's layer for it.
"""
import logging
import urllib.error
from typing import NoReturn, Optional


def raise_bounded_error(error: BaseException, deadline: float, now: float, budget_s: float) -> NoReturn:
    """Normalize deadline expiry even when the socket worker finishes before the joining caller."""
    # Preserve early socket timeouts and unrelated failures as Hub's transport errors.
    if (isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)
            and now >= deadline):
        raise TimeoutError(f"Board request exceeded its {budget_s:g} s total budget") from error
    raise error

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


# A replay-safe timeout is resent only when at least this much of the shared deadline is left: less cannot carry a
# round trip, and spending it would turn the real timeout into a budget-exhausted error.
REPLAY_MIN_BUDGET_S = 0.1


def replay_after_timeout(exc: BaseException, attempt: int, replay_safe: bool, deadline: Optional[float],
                         now: float) -> bool:
    """Whether a timed-out first attempt of a replay-safe request is resent once, on a fresh connection, within the
    same deadline (``now`` is the caller's monotonic clock). Only reserve opts in: it is revision-fenced, and the
    Board answers a same-revision replay with the recorded reservation. Ops and leases never do."""
    if not (replay_safe and attempt == 0 and isinstance(exc, TimeoutError)):
        return False
    if deadline is not None and deadline - now < REPLAY_MIN_BUDGET_S:
        return False
    logging.getLogger(__name__).warning("replay-safe Board request timed out; replaying it once (revision-fenced)")
    return True
