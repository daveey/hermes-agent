"""A pacing-held provider moves a card's route to an unheld peer of the same class. [daveey]

2026-11-15 (hub core 992c0cb5e3b): the openai-codex pool was pacing-``held`` (limit 0) until 11-20 22:40Z but not
walled, so every default-routed card on the fleet waited ~5 days while anthropic, the other eligible deep-class
provider, was ``paced`` with free slots. ``resolve_dispatch_tier`` moved a route only off a wall, and a pacing hold
never produces one: the governor stops exactly the claims whose 429 would record it.

Rule (:func:`paced_route`); anything else keeps the route, as before:

1. the route's concrete provider is stored ``held`` in this home's ``llm_pacing.json`` (the governor's last read;
   ``kanban_llm_pacing_dispatch.held_pools``, no usage read here) and its ``until`` is more than
   ``kanban.llm_pacing.reroute_after_s`` (default 3600) away, or unknown. A ``paced`` pool (limit >= 1) never moves a
   route;
2. the route is an unpinned ``default`` route, a ``review_unpinned`` reviewer, or a non-mandatory concrete pin (the
   far-wall rule of ``kanban_pin_reroute``). Mandatory pins, class selectors and ``require_model: true`` cards keep
   their route;
3. the peer comes from the same eligibility the wall reroutes use (``alternate_route``, the review resolver's
   independent routes, the pin's class peers) and its provider is neither held nor walled. With none, the route
   stays and the governor holds it as before; a moved route is judged by the governor on its new provider.

The move lives on the in-memory claimed task only (as every tier decision does); ``run_route.tier_reason`` names it
``<reason>+paced:<held provider>``.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

REASON = "paced"
DEFAULT_REROUTE_AFTER_S = 3600.0


def _section(cfg: Optional[dict]) -> Any:
    return (cfg or {}).get("llm_pacing")


def reroute_after_seconds(cfg: Optional[dict]) -> float:
    """``kanban.llm_pacing.reroute_after_s``; an unreadable, negative or non-finite value is the default."""
    section = _section(cfg)
    raw = section.get("reroute_after_s", DEFAULT_REROUTE_AFTER_S) if isinstance(section, dict) else DEFAULT_REROUTE_AFTER_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REROUTE_AFTER_S
    return value if math.isfinite(value) and value >= 0 else DEFAULT_REROUTE_AFTER_S


def _held(cfg: Optional[dict], now: float) -> dict[str, float]:
    from hermes_cli.kanban_llm_pacing_dispatch import held_pools
    return held_pools(_section(cfg), now)


def _default_peer(conn, task, base, held, now):
    """The quota-episode probe (unpinned default routes only) with every held pool as a wall; never live-walled."""
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_quota_episode import alternate_route
    route = alternate_route(conn, task, dict(held), base, now)
    if not route or pw.live_quota_wall(route[1], route[0]):
        return None
    return route[0], route[1]


def _review_peer(conn, task, cfg, held, now):
    """The first independent reviewer (``_review_off_wall``'s candidates) on a provider neither held nor walled."""
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_review_model import _implementer_model, independent_routes
    implementer = _implementer_model(conn, task)
    if not implementer:
        return None
    walled = pw.active_walls(now=now)
    for alt_model, alt_provider in independent_routes(implementer, cfg, getattr(task, "assignee", None)):
        alt_provider = pw._norm(alt_provider)
        if (not alt_provider or alt_provider == "auto" or alt_provider in held or alt_provider in walled
                or pw.live_quota_wall(alt_provider, alt_model)):
            continue
        return alt_model, alt_provider
    return None


def _pinned_peer(task, model, held, now):
    from hermes_cli.kanban_pin_reroute import is_mandatory_pin, peer_route
    from hermes_cli.kanban_provider_walls import active_walls
    if is_mandatory_pin(task):
        return None
    return peer_route(task, model, {**active_walls(now=now), **held})


def _probe(conn, task, base, cfg, now):
    """``(model, provider, held provider)`` per the module rule, else None."""
    from hermes_cli import kanban_provider_walls as pw
    reason = base[2]
    if reason == "pinned":
        from hermes_cli.kanban_pin_reroute import pin
        pinned = pin(task)
        if pinned is None:
            return None
        model, provider = pinned
    elif reason in ("default", "review_unpinned"):
        model, provider = base[0], pw._norm(base[1])
        if not model or not provider or pw.requires_model(task):
            return None
    else:
        return None
    held = _held(cfg, now)
    if held.get(provider, -math.inf) - now <= reroute_after_seconds(cfg):
        return None  # not held (a class selector's ``auto`` never is), or its reset is near
    if reason == "pinned":
        moved = _pinned_peer(task, model, held, now)
    elif reason == "default":
        moved = _default_peer(conn, task, base, held, now)
    else:
        moved = _review_peer(conn, task, cfg or {}, held, now)
    return (*moved, provider) if moved else None


def paced_route(conn: sqlite3.Connection, task: Any, base: tuple, *, lane: str,
                cfg: Optional[dict]) -> Optional[tuple[str, str, str]]:
    """``(model, provider, tier_reason)`` moving *base* off a far pacing hold (module rule), else None. ``lane`` is
    the caller's: a review run reaches here as ``review_unpinned``, never as ``pinned``.

    Non-allocating and read-only, so preview, the governor and admission decide alike. A fault keeps the route
    (warned); a SQLite error propagates as the other tier reads do."""
    try:
        moved = _probe(conn, task, base, cfg, time.time())
    except sqlite3.Error:
        raise
    except Exception:  # health: allow BLE001 -- a failed probe keeps the route; the governor still judges it
        logger.warning("kanban paced reroute: probe failed for %s (lane %s); keeping its route",
                       getattr(task, "id", "?"), lane, exc_info=True)
        return None
    if not moved:
        return None
    return moved[0], moved[1], f"{base[2]}+{REASON}:{moved[2]}"


def route_words(conn: sqlite3.Connection, task: Any) -> Optional[str]:
    """``"<tier_reason> -> <provider>/<model>"`` when a ready ``task``'s next run is moved off a held pool (queue
    notes and stall notices name it), else None. Resolves no route while nothing is held (one state-file read)."""
    from hermes_cli.kanban_model_tier import load_tier_config, resolve_dispatch_tier
    cfg = load_tier_config()
    if not _held(cfg, time.time()):
        return None
    model, provider, reason = resolve_dispatch_tier(conn, task, lane="ready", cfg=cfg)
    if f"+{REASON}:" not in str(reason or ""):
        return None
    return f"{reason} -> {provider}/{model}"
