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
2. the route is an unpinned ``default`` route, a ``review_unpinned`` reviewer, an explicit handoff ``review_model``
   (``review_override``/``review_override_refused``), or a non-mandatory concrete pin (the far-wall rule of
   ``kanban_pin_reroute``). Mandatory pins, class selectors and ``require_model: true`` cards keep their route;
3. the peer comes from the same eligibility the wall reroutes use (``alternate_route``, the review resolver's
   independent routes, the pin's class peers) and its provider is neither held nor walled, and serves on this
   host's live pool (``live_pool_serving``: never a provider this host holds no credential for). A review peer is
   never the implementer's model, and an override's peer must also pass this host's routing registry. With none,
   the route stays and the governor holds it as before; a moved route is judged by the governor on its new provider.

The move lives on the in-memory claimed task only (as every tier decision does); ``run_route.tier_reason`` names it
``<reason>+paced:<held provider>``, and a review run's ``model_tier`` receipt adds ``paced`` (:func:`receipt`): the
held provider, its ``until`` and the route it replaced.

2026-11-16 (t_01a1e15ae11c8d41): pool-worker registries list no Fable deep route, so a Claude-implemented review
there has gpt-6-astra as its only independent reviewer and keeps waiting on a codex hold (rule 3: never the
implementer). That is registry policy, not this rule.
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
_MAX_PEER_PROBES = 8  # peers refused for no live pool on this host before the route stays
# Review tier reasons a far hold moves: the unpinned independent reviewer and an explicit handoff
# ``review_model`` (kept or substituted by the registry), each to the next independent registered reviewer.
REVIEW_REASONS = ("review_unpinned", "review_override", "review_override_refused")


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


def _review_peer(conn, task, cfg, held, now, *, registered=False):
    """The first independent reviewer (``_review_off_wall``'s candidates) on a provider neither held nor walled.
    ``registered`` (override fallbacks) also requires this host's routing registry to accept it, as the
    refused-override fallback does: a handoff's own model may sit outside the registry's deep routes."""
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_review_model import _implementer_model, independent_routes, registry_refusal
    implementer = _implementer_model(conn, task)
    if not implementer:
        return None
    assignee = getattr(task, "assignee", None)
    walled = pw.active_walls(now=now)
    for alt_model, alt_provider in independent_routes(implementer, cfg, assignee):
        alt_provider = pw._norm(alt_provider)
        if (not alt_provider or alt_provider == "auto" or alt_provider in held or alt_provider in walled
                or pw.live_quota_wall(alt_provider, alt_model)):
            continue
        if registered and registry_refusal(alt_model, alt_provider, assignee):
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
    elif reason in ("default", *REVIEW_REASONS):
        model, provider = base[0], pw._norm(base[1])
        if not model or not provider or pw.requires_model(task):
            return None
    else:
        return None
    held = _held(cfg, now)
    if held.get(provider, -math.inf) - now <= reroute_after_seconds(cfg):
        return None  # not held (a class selector's ``auto`` never is), or its reset is near
    # The peer must serve on this host's live pool: a registry route with no credential here (xai-oauth on a worker
    # the hub relay has not minted it to) would launch a run that cannot authenticate. Each refusal excludes one more
    # provider, so the walk is bounded. (t_01a1deaf95f5b885)
    excluded = dict(held)
    for _ in range(_MAX_PEER_PROBES):
        if reason == "pinned":
            moved = _pinned_peer(task, model, excluded, now)
        elif reason == "default":
            moved = _default_peer(conn, task, base, excluded, now)
        else:
            moved = _review_peer(conn, task, cfg or {}, excluded, now, registered=reason != "review_unpinned")
        if not moved:
            return None
        target = pw._norm(moved[1])
        if target in excluded:
            return None  # the peer pick ignored its exclusions: never trust it
        if pw.live_pool_serving(target, moved[0]):
            return (*moved, provider)
        excluded[target] = math.inf
    return None


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


def receipt(conn: sqlite3.Connection, task: Any, reason: str, *, lane: str,
            cfg: Optional[dict]) -> Optional[dict[str, Any]]:
    """``{"provider", "until", "from"}`` for a route *reason* that moved off a hold (``...+paced:<provider>``): the
    held provider, its stored reset (None when unknown) and the ``{model, provider}`` it replaced (the read-only
    pre-pacing tier). None for any other reason. Read before the claimed task's route is overwritten."""
    marker = f"+{REASON}:"
    if marker not in str(reason or ""):
        return None
    from hermes_cli.kanban_model_tier import resolve_model_tier
    held = str(reason).rsplit(marker, 1)[1]
    until = _held(cfg, time.time()).get(held)
    base = resolve_model_tier(conn, task, lane=lane, cfg=cfg)
    return {"provider": held, "until": int(until) if until is not None and math.isfinite(until) else None,
            "from": {"model": base[0], "provider": base[1]}}


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
