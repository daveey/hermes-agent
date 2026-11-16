"""Claim-time handoff of a pace reroute from the Kanban quota governor to the route applier. [daveey]

The governor (the dispatcher's pace hold) judges a ready card before its claim; when every pool the card would run on
is ahead of pace but another subscription route of the same class has room, it starts the card on that route
instead of holding it. The route itself is applied after the claim (``kanban_model_tier.apply_model_tier``), so the
governor :func:`offer`\\ s the move here and the applier :func:`take`\\ s it for the same card in the same tick.

An offer binds to the route the governor judged: the applier moves the card only when its own resolution lands on
that same route (else the card's route changed between the two reads and the move no longer applies). Offers live
:data:`TTL_SECONDS` and are taken once. Process-local by design: the hold and the claim run in one dispatcher tick.

An admission that rests on an offer is not an ordinary admission: the governor released the card only because it
would leave its paced pools. So an offer that expired, or no longer matches the resolved route, fails closed
(:class:`OfferLost`): the launch is refused before spawn and the card is requeued for a fresh governor judgment,
never started on the route the governor held. Each judgment first :func:`withdraw`\\ s the card's earlier offer, so
an offer left by a tick whose claim never happened cannot steer a later, ordinary admission.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

TTL_SECONDS = 600.0  # spans claim-time workspace preparation; past it the launch still fails closed (OfferLost)
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


class OfferLost(RuntimeError):
    """The card was admitted on a pace move that can no longer be applied; it must not start on the held route."""


def withdraw(task_id: str) -> None:
    """Drop ``task_id``'s outstanding offer (the governor is judging the card afresh)."""
    with _LOCK:
        _OFFERS.pop(task_id, None)


def take(task_id: str, resolved: tuple) -> Optional[tuple[str, str, str]]:
    """The move offered for ``task_id``, consumed; None when there is no offer (an ordinary admission).

    Raises :class:`OfferLost` when an offer exists but expired or was judged on another route than ``resolved``."""
    with _LOCK:
        found = _OFFERS.pop(task_id, None)
    if found is None:
        return None
    if time.monotonic() - found[0] >= TTL_SECONDS:
        raise OfferLost(f"pace reroute offer for {task_id} expired before launch; requeued for a fresh pace judgment")
    if found[1] != _norm(resolved):
        raise OfferLost(f"pace reroute offer for {task_id} was judged on {found[1]}, launch resolved "
                        f"{_norm(resolved)}; requeued for a fresh pace judgment")
    return found[2]


def lose(task_id: str, why: str) -> None:
    """Fail closed for a card whose launch route could not be resolved: :class:`OfferLost` if it held an offer."""
    with _LOCK:
        found = _OFFERS.pop(task_id, None)
    if found is not None:
        raise OfferLost(f"pace reroute offer for {task_id} cannot be applied ({why}); requeued for a fresh pace "
                        "judgment")


def clear() -> None:
    with _LOCK:
        _OFFERS.clear()
