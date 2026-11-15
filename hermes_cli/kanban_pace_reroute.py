"""Claim-time handoff of a pace reroute from the Kanban quota governor to the route applier. [daveey]

The governor (the dispatcher's pace hold) judges a ready card before its claim; when every pool the card would run on
is ahead of pace but another subscription route of the same class has room, it starts the card on that route
instead of holding it. The route itself is applied after the claim (``kanban_model_tier.apply_model_tier``), so the
governor :func:`offer`\\ s the move here and the applier :func:`take`\\ s it for the same card in the same tick.

An offer binds to the route the governor judged: the applier moves the card only when its own resolution lands on
that same route (else the card's route changed between the two reads and the move no longer applies). Offers live
:data:`TTL_SECONDS` and are taken once. Process-local by design: the hold and the claim run in one dispatcher tick.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

TTL_SECONDS = 120.0
REASON = "paced"  # ``run_route.tier_reason`` suffix: ``<reason>+paced:<paced providers>``
_LOCK = threading.Lock()
_OFFERS: dict[str, tuple[float, tuple[str, str], tuple[str, str, str]]] = {}


def _norm(route: tuple) -> tuple[str, str]:
    model, provider = route[0], route[1]
    return str(model or "").strip(), str(provider or "").strip().lower()


def offer(task_id: str, judged: tuple, moved: tuple[str, str, str]) -> None:
    """Record that ``task_id``'s judged ``(model, provider)`` route moves to ``moved`` = ``(model, provider, paced)``."""
    now = time.monotonic()
    with _LOCK:
        for key in [k for k, v in _OFFERS.items() if now - v[0] >= TTL_SECONDS]:
            _OFFERS.pop(key, None)
        _OFFERS[task_id] = (now, _norm(judged), moved)


def take(task_id: str, resolved: tuple) -> Optional[tuple[str, str, str]]:
    """The move offered for ``task_id`` when it is fresh and was judged on the ``resolved`` route; consumed either way."""
    with _LOCK:
        found = _OFFERS.pop(task_id, None)
    if found is None or time.monotonic() - found[0] >= TTL_SECONDS or found[1] != _norm(resolved):
        return None
    return found[2]


def clear() -> None:
    with _LOCK:
        _OFFERS.clear()
