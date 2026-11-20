"""One board's containment refusal ends only that board's sync round (incident 2026-11-20).

``certify_page`` raises ``ContainmentRefused`` when a feed page's protected containment event does not match its
canonical receipt. That refusal is correct and stays strict: nothing from the page is applied and the board's
cursor does not move. Before this module the exception escaped ``sync_board`` -> ``sync_all_once`` ->
``run_forever``, killed ``kanban_sync_watcher`` and, after the supervisor's five rapid restarts, stopped every
board on the host until the gateway restarted (04:42Z and again 06:31Z on 2026-11-20: hub, worker-1, both Macs).

Here the loop records the refusal as that board's round error, logs it once per distinct refusal, and skips the
board with a doubling backoff (60 s to 15 min) while the other boards keep syncing. A round that completes for the
board clears its state. [daveey]
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

_log = logging.getLogger("hermes_cli.kanban_sync_client")

FIRST_BACKOFF_S = 60.0
MAX_BACKOFF_S = 900.0

_lock = threading.Lock()
# board slug -> (retry_at on the monotonic clock, current backoff seconds, refusal text)
_state: dict[str, tuple[float, float, str]] = {}


def waiting(slug: str, *, now: Optional[Callable[[], float]] = None) -> Optional[str]:
    """The refusal text while ``slug`` is backing off after a refusal; None when it may run."""
    clock = now or time.monotonic
    with _lock:
        entry = _state.get(slug)
    if entry is None or clock() >= entry[0]:
        return None
    return entry[2]


def refused(slug: str, exc: BaseException, *, now: Optional[Callable[[], float]] = None) -> str:
    """Record a refusal for ``slug`` and return the round error text. Logs ERROR only when the refusal text is new
    for this board; a retry that hits the same refusal logs at DEBUG."""
    clock = now or time.monotonic
    text = f"ContainmentRefused: {exc}"
    with _lock:
        prior = _state.get(slug)
        backoff = FIRST_BACKOFF_S if prior is None else min(prior[1] * 2, MAX_BACKOFF_S)
        _state[slug] = (clock() + backoff, backoff, text)
    if prior is None or prior[2] != text:
        _log.error("kanban sync: %s round refused (%s); nothing applied, cursor kept; other boards keep syncing, "
                   "retry in %ds", slug, text, int(backoff))
    else:
        _log.debug("kanban sync: %s still refused (%s); retry in %ds", slug, text, int(backoff))
    return text


def cleared(slug: str) -> None:
    """A round for ``slug`` completed: drop its refusal backoff."""
    with _lock:
        prior = _state.pop(slug, None)
    if prior is not None:
        _log.info("kanban sync: %s syncs again after a containment refusal (%s)", slug, prior[2])


def reset() -> None:
    with _lock:
        _state.clear()
