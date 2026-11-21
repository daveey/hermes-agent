"""A router-chosen pin is not David's pin: a far quota wall moves it within its class. [daveey]

MacBook Desktop chat 20261026_000514_23ef49 (11-02): the router allocated openai-codex/gpt-6-astra for
the chat's ``deep`` class, the gateway's concrete echo of it was stored as an exact pin, and a 33 h
Codex wall then refused every turn although claude-opus-5-5 in the same class was free.

Policy (one place, read by the managed resolver and the gateway's pre-build lookup):
- ``explicit`` pins (an honoured switch, a model David named, a Kanban card's pin) never move;
- a ``default`` pin (router pick, config default, an echo of either) whose subscription wall has a known
  future reset is re-selected in its class (``same-class-failover`` with ``walled_from``), however short
  the wall: a turn refused until the reset is worse than one served by the next eligible route (hub Desktop
  chat 20261111_213321_be8afc, 11-21: a ~1 h fable wall failed three turns while opus was free). The
  reset in the notice is when at least half the pool's accounts can serve again (``decision_reset``);
- once rerouted, the session follows the reroute even while its stored model still names the walled one.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


def _state_path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "model-routing.sqlite3"


def peek_route(session_id, level):
    """The session's standing route in the real store (read-only; never the pre-build ``:memory:``)."""
    if not session_id:
        return None
    from agent.model_routing_state import RoutingState
    try:
        return RoutingState(_state_path()).peek(session_id, level)
    except Exception:  # an unreadable store proves nothing: treat as no row (the pin then holds)
        logger.debug("routing: could not peek %s/%s", session_id, level, exc_info=True)
        return None


def request_pin_source(model, provider, config, replace: bool | str = False):
    """``explicit`` for a model David named (switch, -m, picker) or a Kanban card; ``default`` when the
    request is the config default. An existing row decides for itself (an echo never upgrades it)."""
    if replace or os.environ.get("HERMES_KANBAN_TASK"):
        return "explicit"
    model_cfg = (config or {}).get("model") or {}
    default = model_cfg.get("default") if isinstance(model_cfg, dict) else model_cfg
    default_provider = model_cfg.get("provider") if isinstance(model_cfg, dict) else None
    if model == default and provider in (None, "", "auto", default_provider):
        return "default"
    return "explicit"


def follows_reroute(row, model):
    """``(provider, model)`` the session was rerouted to when *model* is the one its wall moved it off."""
    from agent.model_routing_state import is_explicit_pin
    walled = (row or {}).get("walled_from") or {}
    if is_explicit_pin(row) or walled.get("model") != model or row.get("selected_model") == model:
        return None
    return row["selected_provider"], row["selected_model"]


def decision_reset(wall):
    """The reset a hold-or-move decision reads: half the pool back, else the wall's only (nearest) reset."""
    pool_reset = getattr(wall, "pool_resets_at", None)
    if isinstance(pool_reset, (int, float)) and not isinstance(pool_reset, bool):
        return pool_reset
    return getattr(wall, "resets_at", None)


def may_reroute(wall, row, source, now=None):
    """True when the quota *wall* on a pin with standing *row* / request *source* should move it.

    Any wall with a known future reset moves a non-explicit pin: a turn that fails until the reset is worse
    than one served by the next eligible route of the pin's class (hub Desktop chat 20261111_213321_be8afc,
    11-21: fable's wall resets in ~1 h, every turn refused, opus free)."""
    from agent.model_routing_state import is_explicit_pin
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False  # card pins: t_01a19990897b9d9b (David's D1) owns their policy
    reset = decision_reset(wall)
    if not isinstance(reset, (int, float)) or reset <= (time.time() if now is None else now):
        return False
    if row is not None:
        return not is_explicit_pin(row)
    return source == "default"


def class_config(config, level):
    """*config* with every class route of *level* (the managed policy already forbids metered ones)."""
    from copy import deepcopy
    full = deepcopy(config)
    full["model_routing"]["routes"] = {level: list(config["model_routing"].get("routes", {}).get(level, []))}
    return full


def reset_pt(resets_at):
    """``Tue 11-03 22:37 PT``: David reads Pacific time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(float(resets_at), ZoneInfo("America/Los_Angeles")).strftime("%a %m-%d %H:%M PT")


def _label(model):
    return model


def reroute_notice(wall, to_model=None):
    """The one chat line for a rerouted turn."""
    target = _label(to_model) if to_model else "another model in its class"
    return f"{_label(wall.model)} quota is out until {reset_pt(wall.resets_at)}; continuing on {target}."


def walled_pin_reply(provider, model, resets_at):
    """The sentence for a chat pinned to *provider*/*model* whose subscription quota is out until *resets_at*.

    One wording for the pre-build refusal (``held_reply``) and a build that hit the wall itself
    (``tui_gateway.user_messages.agent_init_failed_message``)."""
    return (f"⚠️ {provider}/{model} subscription quota is out until {reset_pt(resets_at)}. "
            f"This chat is pinned to it; use /model to switch, or resend after the reset.")


def _route_name(provider, model):
    return f"{provider}/{model}" if model else provider


def walled_chain_reply(primary, routes):
    """The sentence for a pinned *primary* ``(provider, model, resets_at, kind)`` whose fallback *routes* (same
    shape) are walled too: the pin "is rate-limited until" (a rate-limited ``AuthError``) or its "subscription
    quota is out until" (a typed refusal), then each fallback. "Resend after" is the earliest reset across the
    chain, the first moment a route can serve. A pin with no model is named by its provider alone."""
    from agent.model_routing_unavailable import WALL_RATE_LIMIT

    provider, model, resets_at, kind = primary
    state = "is rate-limited" if kind == WALL_RATE_LIMIT else "subscription quota is out"
    fallbacks = ", ".join(f"{_route_name(p, m)} until {reset_pt(r)}" for p, m, r, _ in routes)
    also = f", and its fallbacks are out too ({fallbacks})" if routes else ""
    earliest = min([resets_at, *(r for _, _, r, _ in routes)])
    return (f"⚠️ {_route_name(provider, model)} {state} until {reset_pt(resets_at)}{also}. This chat is pinned "
            f"to it; use /model to switch, or resend after {reset_pt(earliest)}.")


def walled_reply(walls):
    """``walled_pin_reply`` for one quota-walled route, ``walled_chain_reply`` otherwise (a pin plus walled
    fallbacks, or a rate-limited pin whose fallbacks all repeated it)."""
    from agent.model_routing_unavailable import WALL_QUOTA

    primary, *routes = walls
    if not routes and primary[3] == WALL_QUOTA:
        return walled_pin_reply(*primary[:3])
    return walled_chain_reply(primary, routes)


def held_reply(wall):
    """The reply for a walled pin that holds (explicit, or a wall under the reroute threshold)."""
    return walled_pin_reply(wall.provider, wall.model, wall.resets_at)


def log_reroute(wall, metadata):
    logger.warning("routing: walled default pin rerouted %s/%s→%s/%s until %s", wall.provider, wall.model,
                   metadata["selected_provider"], metadata["selected_model"], reset_pt(wall.resets_at))


_NOT_DEFERRED_SEEN: set = set()


def log_not_deferred(wall, reason):
    """One WARNING per wall episode and reason when the pre-build lookup keeps a far wall's refusal.

    A wall with no known future reset stays quiet; any other one that refuses every turn names why (``key``,
    ``endpoint``, ``explicit``, ``no-target``, ``no-session``)."""
    reset, held_until = getattr(wall, "resets_at", None), decision_reset(wall)
    if (not isinstance(reset, (int, float)) or not isinstance(held_until, (int, float))
            or held_until <= time.time()):
        return
    episode = (getattr(wall, "provider", None), getattr(wall, "model", None), int(reset), reason)
    if episode in _NOT_DEFERRED_SEEN:
        return
    _NOT_DEFERRED_SEEN.add(episode)
    emit = logger.error if reason == "no-session" else logger.warning
    emit("routing: walled pin not deferred (reason=%s) %s/%s until %s", reason, wall.provider,
         wall.model, reset_pt(reset))


def predicted_target(config, level, wall):
    """Other ``(provider, model)`` routes that can serve *level* now (the allocator picks among them)."""
    from agent.credential_pool import load_pool
    from agent.model_routing import eligible_routes
    policy = config["model_routing"]
    found = set()
    for route in eligible_routes(policy, level):
        if (route["provider"], route["model"]) == (wall.provider, wall.model) or route["billing"] != "subscription":
            continue
        try:
            if load_pool(route["provider"], allowed_auth_types={"oauth"}).has_available(model=route["model"]):
                found.add((route["provider"], route["model"]))
        except Exception:  # an unreadable pool cannot serve; the wall then stands
            logger.debug("routing: pool %s unreadable", route["provider"], exc_info=True)
            continue
    return sorted(found)


def gateway_deferral(wall, model, provider, config):
    """Pre-build lookup (gateway): a deferred runtime when the agent build will reroute this walled pin,
    else None (the refusal stands). Needs the turn's ``routing_session``."""
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import managed_config, pin_policy
    from agent.model_routing_runtime import current_routing_session
    session_id = current_routing_session()
    if not session_id:
        log_not_deferred(wall, "no-session")
        return None
    managed = managed_config(config)
    if managed is None:
        return None
    try:
        level, _ = pin_policy(model, provider, managed)
    except ModelRoutingError:
        return None
    row = peek_route(session_id, level)
    followed = follows_reroute(row, model)
    if followed is None and not may_reroute(wall, row, request_pin_source(model, provider, managed)):
        log_not_deferred(wall, "explicit")
        return None
    if followed is None:
        targets = predicted_target(managed, level, wall)
        if not targets:
            log_not_deferred(wall, "no-target")
            return None  # nothing else in the class can serve: the refusal stands
        notice = reroute_notice(wall, targets[0][1] if len({p for p, _ in targets}) == 1 else None)
    else:
        notice = None  # the reroute was announced when it happened
    return {"provider": "auto", "requested_provider": "auto", "model": model, "class_deferred": True,
            "api_key": None, "base_url": None, "api_mode": None, "walled_notice": notice}
