"""A far quota wall moves a Kanban card's model pin to a same-class peer for one run at a time. [daveey]

paintbot-tools M4 (t_01a19715a1a9ccef, 11-01) was pinned to openai-codex/gpt-6-astra. Its run hit a
Codex subscription wall resetting ~56 h out, and the quota episode held the card for the whole wall:
an explicit pin had no fallback, and workers cannot re-pin a card. David, 11-11 (t_01a19990897b9d9b):
"Let them move to another provider of the same class."

Rule (:func:`pinned_route`); every condition must hold, anything else keeps the pin as before:

1. one of the card's own runs hit a wall on the pin's provider (its ``quota_episode`` receipts, or the
   live episode), and that wall is still up;
2. it resets ``kanban.pin_quota_reroute_min_hours`` (default 6) or more from now;
3. the pin is not mandatory: neither ``tasks.model_pin_mandatory`` nor the ``require_model`` marker;
4. the lane is ready, not review;
5. the pinned model sits in exactly one routing class of the assignee's policy, and a route of that
   class is eligible, on no walled or disabled provider, and not quota-walled in the live pool.
   There is never a fallback to ``kanban.default_model`` or the strong tier.

Rule 1 reads the card's history, not only the live episode: a rerouted run that ends (continued,
reclaimed, blocked) closes the episode, and the next run must not go back into a wall the card knows.

The reroute lives on the in-memory claimed task only: the row's pin is never written, so the first
run after the reset goes back to the pin. Every rerouted admission records a ``model_reroute`` event;
the card gets one comment per wall (:func:`record`).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_MIN_HOURS = 6.0
REASON = "pin_quota_reroute"
EVENT = "model_reroute"
COMMENT_AUTHOR = "kanban-dispatcher"


def _norm(value: Any) -> str:
    return str(value or "").strip()


def is_mandatory_pin(task: Any) -> bool:
    """True when the card's creator said its pin must never move."""
    from hermes_cli.kanban_provider_walls import REQUIRE_MODEL_MARKER
    if getattr(task, "model_pin_mandatory", None):
        return True
    return REQUIRE_MODEL_MARKER in f"{getattr(task, 'title', '') or ''}\n{getattr(task, 'body', '') or ''}"


def min_wall_seconds(cfg: Optional[dict]) -> float:
    """``kanban.pin_quota_reroute_min_hours`` in seconds; an unreadable value is the default."""
    raw = (cfg or {}).get("pin_quota_reroute_min_hours", DEFAULT_MIN_HOURS)
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        logger.warning("kanban pin reroute: bad kanban.pin_quota_reroute_min_hours %r; using %s", raw,
                       DEFAULT_MIN_HOURS)
        hours = DEFAULT_MIN_HOURS
    if hours != hours or hours < 0:  # NaN or negative
        hours = DEFAULT_MIN_HOURS
    return hours * 3600


def pin(task: Any) -> Optional[tuple[str, str]]:
    """The card's concrete ``(model, provider)`` pin, or None (unpinned, class pin, unknown provider)."""
    from agent.model_routing import is_class_selector
    model, provider = _norm(getattr(task, "model_override", None)), _norm(getattr(task, "provider_override", None))
    provider = provider.lower()
    if not model or not provider or provider == "auto" or is_class_selector(model):
        return None
    return model, provider


def own_walls(conn: Any, task_id: str, now: float,
              current: Optional[dict[str, float]] = None) -> dict[str, tuple[float, Any]]:
    """``provider -> (until, episode_id)`` for every wall this card's own runs hit that is still up.

    Read from the card's ``quota_episode`` receipts; ``current`` adds the live episode's walls (its
    receipt may not carry them yet)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_episode import _providers, episode
    out: dict[str, tuple[float, Any]] = {}

    def add(name: str, until: Any, episode_id: Any) -> None:
        if isinstance(until, (int, float)) and until > now and (name not in out or until > out[name][0]):
            out[name] = (float(until), episode_id)
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode'", (task_id,)):
        state = kb._json_dict(row[0])
        for name in _providers(state):
            add(name, state.get("until"), state.get("episode_id"))
    if current:
        live = episode(conn, task_id) or {}
        for name, until in current.items():
            add(name, until, live.get("episode_id"))
    return out


def pinned_route(conn: Any, task: Any, *, lane: str, now: float, cfg: Optional[dict],
                 current: Optional[dict[str, float]] = None) -> Optional[tuple[str, str]]:
    """Same-class ``(model, provider)`` this pinned card's run may use instead of its walled pin, or None.

    ``current`` is the live quota episode's walls. Other cards' walls (the provider wall files and the
    live pool) only rule peers out, never move a pin. Non-allocating: reads config and the credential
    pool and writes nothing, so preview and admission decide alike."""
    from agent.model_routing import ModelRoutingError, eligible_routes, validate_policy
    from agent.model_routing_agent import class_requirements
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_provider_walls import active_walls, live_quota_wall
    pinned = pin(task)
    if lane == "review" or pinned is None or is_mandatory_pin(task):
        return None
    model, provider = pinned
    own = own_walls(conn, task.id, now, current)
    if provider not in own or own[provider][0] - now < min_wall_seconds(cfg):
        return None
    config = _assignee_config(getattr(task, "assignee", None)) or {}
    policy = config.get("model_routing") or {}
    try:
        validate_policy(policy)
    except ModelRoutingError:
        return None
    if not policy.get("enabled"):
        return None
    # The pin's strength is its class; a model in no class (or in two) has no peer we can prove equal.
    levels = [key for key, routes in policy.get("routes", {}).items() if any(r["model"] == model for r in routes)]
    if len(levels) != 1:
        return None
    walled = {**active_walls(now=now), **{name: wall[0] for name, wall in own.items()}}
    providers = config.get("providers") or {}
    routes = eligible_routes(policy, levels[0], class_requirements(config), eligible=lambda r: (
        r["provider"] not in walled and is_provider_enabled(providers.get(r["provider"], {}))))
    for route in routes:
        if live_quota_wall(route["provider"], route["model"]) is None:
            return route["model"], route["provider"]
    return None


def _pt(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M PT")


def record(conn: Any, task: Any, origin: tuple[str, str], target: tuple[str, str], *,
           now: Optional[float] = None) -> dict:
    """Write the ``model_reroute`` event for this run and, once per wall, a card comment.

    Runs inside the caller's write transaction. Returns the event payload."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_episode import _outstanding, _providers, episode
    now = time.time() if now is None else now
    state = episode(conn, task.id)
    current: dict[str, float] = {}
    for wall in (_outstanding(conn, task.id, state, now) if state else []):
        if origin[1] in _providers(wall):
            current[origin[1]] = max(current.get(origin[1], 0), wall["until"])
    resets_at, episode_id = own_walls(conn, task.id, now, current).get(origin[1], (None, None))
    payload = {"from_model": origin[0], "from_provider": origin[1], "to_model": target[0],
               "to_provider": target[1], "walled_provider": origin[1], "resets_at": resets_at,
               "episode_id": episode_id, "run_id": getattr(task, "current_run_id", None)}
    earlier = [kb._json_dict(row[0]) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task.id, EVENT))]
    kb._append_event(conn, task.id, EVENT, payload, run_id=payload["run_id"])
    if any(p.get("episode_id") == episode_id and p.get("walled_provider") == origin[1] for p in earlier):
        return payload
    until = f"until {_pt(resets_at)}" if resets_at else "until its quota resets"
    kb.add_comment(conn, task.id, COMMENT_AUTHOR, (
        f"Quota wall: {origin[0]} ({origin[1]}) is out {until}. This card's runs use {target[0]} "
        f"({target[1]}), the same model class, until then; the pin itself is unchanged and the first run "
        f"after the reset uses it again. To make this card wait for its pin instead: "
        f"`hermes kanban set-model {task.id} {origin[0]} --provider {origin[1]} --mandatory`."))
    return payload


def mandatory_column(pin_mandatory: Any, model_override: Optional[str]) -> Optional[int]:
    """``tasks.model_pin_mandatory`` for a new card: 1 or NULL. A mark needs a pin to mark."""
    if not pin_mandatory:
        return None
    if not _norm(model_override):
        raise ValueError("pin_mandatory requires a model pin")
    return 1


def set_pin_mandatory(conn: Any, task_id: str, mandatory: bool) -> bool:
    """Mark (or unmark) a card's model pin as mandatory: a quota wall never moves a mandatory pin."""
    from hermes_cli import kanban_db as kb
    if mandatory:
        row = conn.execute("SELECT model_override FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is not None and not _norm(row[0]):
            raise ValueError("--mandatory needs a model pin: set a model first")
    value = 1 if mandatory else None
    return kb._set_task_override(
        conn, task_id, "UPDATE tasks SET model_pin_mandatory = ? WHERE id = ?", (value,),
        "model_pin_mandatory_set", {"mandatory": bool(mandatory)}, ("model_pin_mandatory",),
        archived_msg="cannot change the model pin")


def set_model_cli(conn: Any, task_id: str, raw_model: Optional[str], model: Optional[str],
                  provider: Optional[str], mandatory: Optional[bool]) -> tuple[bool, bool]:
    """``hermes kanban set-model``: ``(found, wrote_model)``. A bare ``--[no-]mandatory`` changes only the
    mark (``set-model <id>`` alone still clears the pin, as before)."""
    from hermes_cli import kanban_db as kb
    if raw_model is None and mandatory is not None:
        return set_pin_mandatory(conn, task_id, mandatory), False
    found = kb.set_model_override(conn, task_id, model, provider=provider)
    if found and mandatory is not None:
        set_pin_mandatory(conn, task_id, mandatory)
    return found, True
