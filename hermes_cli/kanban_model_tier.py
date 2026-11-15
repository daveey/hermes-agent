"""Kanban default model tier [daveey].

When ``kanban.default_model`` is set, the dispatcher runs a card that has no pinned model on that model
(typically a cheaper tier) instead of the assignee profile's own model. The pin is applied to the in-memory
task only (``model_override``/``provider_override`` on the claimed ``Task``), so the card row never changes
and a later run re-decides. Cards keep the profile's model when:

* the card pins ``model`` itself, except review runs choose an independent reviewer;
* it runs in the review lane (``sdlc-review`` loaded — claimed from the review column);
* an earlier run on the card failed (``crashed``/``timed_out``/``gave_up``/``failed``) or a reviewer asked for
  changes: the second attempt after a failed fix gets the stronger model;
* its title/body matches one of ``kanban.default_model_keep_patterns`` (case-insensitive regexes);
* its assignee is not in ``kanban.default_model_profiles`` (empty = every profile).

``kanban.strong_model`` (+ optional ``kanban.strong_provider``, else inferred from the model) names the model
those review/retry/keep-pattern cards run on instead of the profile's model; unset keeps the old behaviour.

Unset ``kanban.default_model`` retains profile routing except for explicit review pins/overrides.
Review handoff metadata ``review_model`` (optional ``review_provider``) wins unless this host's routing registry
refuses it (``review_override_refused``: a registered reviewer distinct from the implementer, the request kept in
the receipt); otherwise concrete author pins choose a distinct strong/deep model.
Ready/retry pins and unpinned out-of-scope profiles are unchanged.
Each decision is recorded as a ``model_tier`` event, with ``implementer_model`` on review decisions.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Run outcomes that mean "the previous attempt did not work" (not a checkpoint, a human block or a reclaim).
FAILED_RUN_OUTCOMES = ("crashed", "oom_killed", "signaled", "timed_out", "gave_up", "failed")
# Events that mean "a reviewer rejected the previous attempt".
REWORK_EVENT_KINDS = ("changes_requested", "review_reopened")
REVIEW_SKILL = "sdlc-review"


def _str(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [_str(v) for v in value if _str(v)]
    return []


def load_tier_config() -> dict[str, Any]:
    """The ``kanban`` config section (empty dict when unreadable)."""
    try:
        from hermes_cli.config import load_config
        section = (load_config() or {}).get("kanban") or {}
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # never break dispatch over config
        logger.debug("kanban model tier: config unreadable (%s)", exc)
        return {}


def _prior_failure(conn: sqlite3.Connection, task_id: str, current_run_id: Optional[int]) -> Optional[str]:
    marks = ",".join("?" for _ in FAILED_RUN_OUTCOMES)
    row = conn.execute(
        f"SELECT outcome FROM task_runs WHERE task_id = ? AND id IS NOT ? AND outcome IN ({marks}) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, current_run_id, *FAILED_RUN_OUTCOMES),
    ).fetchone()
    if row is not None:
        return f"run_{row[0]}"
    marks = ",".join("?" for _ in REWORK_EVENT_KINDS)
    row = conn.execute(
        f"SELECT kind FROM task_events WHERE task_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
        (task_id, *REWORK_EVENT_KINDS),
    ).fetchone()
    return row[0] if row is not None else None


def resolve_model_tier(
    conn: sqlite3.Connection, task: Any, *, lane: str, cfg: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str], str]:
    """``(model, provider, reason)`` for a claimed task. ``model`` is None when the worker keeps whatever it
    would have run on anyway (its own pin or the profile's model); ``reason`` names the rule that decided."""
    cfg = load_tier_config() if cfg is None else cfg
    default_model = _str(cfg.get("default_model"))
    if lane == "review" or REVIEW_SKILL in (getattr(task, "skills", None) or []):
        from hermes_cli.kanban_review_model import resolve_review_model
        review = resolve_review_model(conn, task, cfg)
        if review is not None:
            return review
    if _str(getattr(task, "model_override", None)):
        return None, None, "pinned"
    if not default_model:
        return None, None, "off"
    profiles = _as_list(cfg.get("default_model_profiles"))
    if profiles and _str(getattr(task, "assignee", None)) not in profiles:
        return None, None, "profile_out_of_scope"
    # Class defaults opt into class-preserving routing. Strong work must not
    # fall back to a profile that may itself now default to standard/fast.
    from agent.model_routing import is_class_selector
    strong_model = _str(cfg.get("strong_model"))
    strong: tuple[Optional[str], Optional[str]]
    if strong_model:
        # An explicit strong tier names its model, so review/retry work no longer follows whatever the
        # profile's default happens to be (a profile switch once moved 558 strong runs to a GPT).
        strong = (strong_model, _str(cfg.get("strong_provider")) or infer_provider(strong_model))
    else:
        strong = ("class:deep", "auto") if is_class_selector(default_model) else (None, None)
    if lane == "review" or REVIEW_SKILL in (getattr(task, "skills", None) or []):
        return *strong, "review"
    try:
        failure = _prior_failure(conn, task.id, getattr(task, "current_run_id", None))
    except sqlite3.Error as exc:
        logger.debug("kanban model tier: prior-failure lookup failed for %s (%s)", task.id, exc)
        failure = None
    if failure:
        return *strong, f"retry_after_{failure}"
    text = f"{getattr(task, 'title', '') or ''}\n{getattr(task, 'body', '') or ''}"
    for pattern in _as_list(cfg.get("default_model_keep_patterns")):
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return *strong, f"pattern:{pattern}"
        except re.error as exc:
            logger.warning("kanban model tier: bad default_model_keep_patterns entry %r (%s)", pattern, exc)
    return default_model, _str(cfg.get("default_provider")) or infer_provider(default_model), "default"


def infer_provider(model: str) -> Optional[str]:
    """Provider for a tier model when ``default_provider`` is unset. Without one the worker pairs the tier model
    with the PROFILE's provider, so ``claude-sonnet-5-5`` was sent to openai-codex (HTTP 400, every card dead on
    its first run) after the profile model was switched to a GPT. Static catalog first, then the model family."""
    try:
        from hermes_cli.models import detect_static_provider_for_model
        hit = detect_static_provider_for_model(model, "")
        if hit:
            return hit[0]
    except Exception as exc:  # never break dispatch over the catalog
        logger.debug("kanban model tier: provider lookup failed for %s (%s)", model, exc)
    return "anthropic" if model.lower().startswith("claude") else None


def class_unroutable_reason(model: str, config: Optional[dict[str, Any]] = None) -> Optional[str]:
    """None when this host can route ``class:<level>``; otherwise a short reason.

    Non-allocating config check with the allocator's own billing/capability filter
    (``agent.model_routing.eligible_routes``) plus the provider-enabled gate the worker applies. Credential
    exhaustion is not checked here: that is a transient wall handled by ``kanban_provider_walls``.
    Unreadable config returns None (unknown = do not change the decision).
    """
    from agent.model_routing import CLASSES, ModelRoutingError, eligible_routes, validate_policy
    level = str(model).removeprefix("class:")
    if config is None:
        try:
            from hermes_cli.config_effective import load_user_config_effective
            config = load_user_config_effective() or {}
        except Exception as exc:  # never break dispatch over config
            logger.debug("kanban model tier: routing config unreadable (%s)", exc)
            return None
    policy = config.get("model_routing") or {}
    try:
        validate_policy(policy)
    except ModelRoutingError:
        return "invalid_policy"
    if not policy.get("enabled", False):
        return "routing_disabled"
    if level not in CLASSES:
        return "unknown_class"
    from hermes_cli.config import is_provider_enabled
    providers = config.get("providers") or {}

    def enabled(route: dict) -> bool:
        block = providers.get(route["provider"], {}) if isinstance(providers, dict) else {}
        return is_provider_enabled(block if isinstance(block, dict) else {})

    if not eligible_routes(policy, level, eligible=enabled):
        return "no_eligible_routes"
    return None


def worker_model_args(model: str, provider: Optional[str]) -> list[str]:
    """Keep class allocation inside the owning worker, never the dispatcher.

    An explicit auto provider prevents the worker profile's concrete provider
    from turning a class request into a backend pin.
    """
    from agent.model_routing import ModelRoutingError, is_class_selector
    if is_class_selector(model):
        if provider and provider != "auto":
            raise ModelRoutingError("Class model selectors require provider auto")
        provider = "auto"
    args = ["-m", model]
    if provider:
        args.extend(["--provider", provider])
    return args


def owner_model_pin(conn: sqlite3.Connection, task: Any) -> tuple[Optional[str], Optional[str]]:
    """``(model, provider)`` the profile owner runs *task*'s current run with. [daveey]

    Workers are ``-m hermes_cli.kanban_worker_client`` (upstream) and the owner re-reads the card, so the
    dispatcher's in-memory tier pin (:func:`apply_model_tier`) reaches it only through this run's
    ``model_tier`` event; without one the row's overrides apply. A class selector always runs with
    provider ``auto`` (:func:`worker_model_args`), so the profile's concrete provider never pins a class."""
    import json
    model, provider = _str(getattr(task, "model_override", None)) or None, _str(getattr(task, "provider_override", None)) or None
    run_id = getattr(task, "current_run_id", None)
    if run_id is not None:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='model_tier' "
                           "ORDER BY id DESC LIMIT 1", (task.id, run_id)).fetchone()
        try:
            payload = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            payload = {}
        from hermes_cli import kanban_db as _kb
        review = REVIEW_SKILL in (getattr(task, "skills", None) or []) or (
            _kb._retry_status_for_run(conn, task.id, run_id) == "review")
        if review and model and (not isinstance(payload, dict) or not _str(payload.get("model"))):
            from hermes_cli.kanban_review_model import ReviewModelUnavailable
            raise ReviewModelUnavailable("Review owner requires a current-run model_tier receipt")
        if isinstance(payload, dict) and _str(payload.get("model")):
            model, provider = _str(payload["model"]), _str(payload.get("provider")) or None
    if not model:
        return None, None
    args = worker_model_args(model, provider)
    return model, (args[args.index("--provider") + 1] if "--provider" in args else None)


RUN_ROUTE_EVENT = "run_route"


def profile_route(assignee: Any) -> tuple[Optional[str], Optional[str]]:
    """``(model, provider)`` from the assignee profile's ``model`` block: what an unpinned worker runs.

    A class selector or ``auto`` provider leaves the provider unknown (the worker allocates it).
    Unreadable or non-profile config is ``(None, None)``: unknown, never a guess."""
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_assignment import _assignee_config
    config = _assignee_config(assignee) or {}
    block = config.get("model") if isinstance(config, dict) else None
    if isinstance(block, dict):
        model, provider = _str(block.get("default")) or None, _str(block.get("provider")) or None
    elif isinstance(block, str):
        model, provider = _str(block) or None, None
    else:
        return None, None
    if not model or is_class_selector(model) or (provider or "").lower() == "auto":
        provider = None
    return model, provider


def effective_route(task: Any) -> tuple[Optional[str], Optional[str], str]:
    """``(model, provider, source)`` a claimed task's worker runs with after :func:`apply_model_tier`."""
    from agent.model_routing import is_class_selector
    model = _str(getattr(task, "model_override", None)) or None
    provider = _str(getattr(task, "provider_override", None)) or None
    if model:
        if is_class_selector(model) or (provider or "").lower() == "auto":
            return model, None, "class"
        return model, provider, "pin" if provider else "pin_provider_unknown"
    model, provider = profile_route(getattr(task, "assignee", None))
    return model, provider, "profile_default" if model else "unknown"


def record_run_route(conn: sqlite3.Connection, task: Any, *, lane: str, reason: str) -> None:
    """Admission receipt (``run_route`` event on this run) for every spawn, tier on or off. [daveey]

    Shared-blocker scope (``kanban_shared_blockers._run_route``) reads it as the run's historical route;
    the card's later pins and today's config never rewrite it. Unknown stays None. Never raises."""
    run_id = getattr(task, "current_run_id", None)
    if run_id is None:
        return
    try:
        model, provider, source = effective_route(task)
    except Exception as exc:
        logger.debug("kanban model tier: route resolve failed for %s (%s)", getattr(task, "id", "?"), exc,
                     exc_info=True)
        model, provider, source = None, None, "unknown"
    try:
        from hermes_cli import kanban_db as _kb
        with _kb.write_txn(conn):
            _kb._append_event(conn, task.id, RUN_ROUTE_EVENT, {
                "model": model, "provider": provider, "lane": lane, "source": source, "tier_reason": reason,
            }, run_id=run_id)
    except Exception as exc:
        logger.debug("kanban model tier: route receipt failed for %s (%s)", getattr(task, "id", "?"), exc,
                     exc_info=True)


def apply_model_tier(conn: sqlite3.Connection, task: Any, *, lane: str, cfg: Optional[dict[str, Any]] = None) -> str:
    """Pin the tier's model on the in-memory ``task`` (never the DB row) and record a ``model_tier`` event,
    plus the run's resolved ``run_route`` receipt (every spawn, tier off included).
    Returns the reason. Review resolution/receipt failures refuse admission rather than reusing an author pin."""
    reason = _apply_model_tier(conn, task, lane=lane, cfg=cfg)
    record_run_route(conn, task, lane=lane, reason=reason)
    return reason


def resolve_dispatch_tier(conn, task, *, lane, cfg=None):
    """One read-only route decision for preview and actual admission."""
    from hermes_cli.kanban_quota_episode import active, decision
    base = resolve_model_tier(conn, task, lane=lane, cfg=cfg)
    _, route = decision(conn, task, lane=lane, cfg=cfg, base=base)
    if base[2] == 'pinned':
        from hermes_cli.kanban_pin_reroute import REASON, pinned_route
        if not route and not active(conn, task.id):
            # No live episode (none yet, or a rerouted run ended) but the pin's wall may still be up: the card's
            # own, or one the provider file / live pool knows (kanban_pin_reroute rule 1).
            import time
            try:
                route = pinned_route(conn, task, lane=lane, now=time.time(),
                                     cfg=load_tier_config() if cfg is None else cfg)
            except sqlite3.Error:
                raise
            except Exception:
                logger.warning('kanban pin reroute: resolve failed for %s; keeping the pin', task.id, exc_info=True)
        if route:
            return *route, f'pinned+{REASON}:{str(task.provider_override).strip().lower()}'
    if route:
        return *route, f'{base[2]}+quota_reroute'
    if active(conn, task.id):
        return base
    # An expired episode no longer judges the route: another card's (or the live pool's) wall still does.
    # Legacy provider-wide walls may only change ordinary default routing.
    if base[2] == 'default' and not task.provider_override:
        from hermes_cli.kanban_provider_walls import retier_for_walls
        from copy import copy
        probe = copy(task)
        probe.model_override, probe.provider_override = base[:2]
        moved = _class_alternate_off_wall(conn, task, base) or retier_for_walls(
            probe, load_tier_config() if cfg is None else cfg)
        if moved:
            return moved[0], moved[1], f'{base[2]}+wall:{moved[2]}'
    if base[2] == 'review_unpinned':
        moved = _review_off_wall(conn, task, base, load_tier_config() if cfg is None else cfg)
        if moved:
            return moved[0], moved[1], f'{base[2]}+wall:{moved[2]}'
    # No wall, but the route's pool is pacing-held for longer than reroute_after_s: an unheld class peer runs it.
    from hermes_cli.kanban_paced_reroute import paced_route
    return paced_route(conn, task, base, lane=lane, cfg=load_tier_config() if cfg is None else cfg) or base


def _review_off_wall(conn, task, base, cfg):
    """``(model, provider, walled_provider)``: another independent reviewer when the ``review_unpinned`` route's
    provider is walled right now (provider file or live pool), else None. [daveey]

    Candidates are the ones the review resolver itself would pick (strong tier, then deep routes distinct
    from the implementer), skipping any on a walled provider. None keeps the route: never a second wall.
    Hub run 174730 (2026-11-12): review_unpinned chose openai-codex while provider_walls.json held it for
    days, and the reviewer exited 75 into the wall."""
    import time
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_review_model import _implementer_model, independent_routes
    model, provider = _str(base[0]), pw._norm(base[1])
    if not model or not provider or provider == 'auto' or pw.requires_model(task):
        return None
    now = time.time()
    walled = pw.active_walls(now=now)
    if not (walled.get(provider) or pw.live_quota_wall(provider, model)):
        return None
    implementer = _implementer_model(conn, task)
    if not implementer:
        return None
    for alt_model, alt_provider in independent_routes(implementer, cfg, getattr(task, 'assignee', None)):
        alt_provider = pw._norm(alt_provider)
        if (not alt_provider or alt_provider == 'auto' or alt_provider == provider or alt_provider in walled
                or pw.live_quota_wall(alt_provider, alt_model)):
            continue
        return alt_model, alt_provider, provider
    return None


def _class_alternate_off_wall(conn, task, base):
    """``(model, provider, walled_provider)`` from the assignee's routing class when *base*'s provider is walled
    right now (provider file or live pool), else None. [daveey]

    Same eligibility probe a card's own quota episode uses (``alternate_route``). Without it a fresh card on a
    walled default provider had no fallback when the strong tier is a class selector (``retier_for_walls``
    skips ``auto``): every first claim spawned into the wall, exited 75, and only its new episode rerouted."""
    import time
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_quota_episode import alternate_route
    model, provider = base[0], pw._norm(base[1])
    if pw.requires_model(task):
        return None
    if not model or not provider or provider == 'auto':
        return None
    now = time.time()
    until = pw.active_walls(now=now).get(provider) or pw.live_quota_wall(provider, model)
    if not until:
        return None
    route = alternate_route(conn, task, {provider: until}, base, now)
    if not route or pw.live_quota_wall(route[1], route[0]):
        return None
    return route[0], route[1], provider


def _apply_model_tier(conn: sqlite3.Connection, task: Any, *, lane: str, cfg: Optional[dict[str, Any]] = None) -> str:
    implementer_model = _str(getattr(task, "model_override", None)) or None
    is_review = lane == "review" or REVIEW_SKILL in (getattr(task, "skills", None) or [])
    try:
        model, provider, reason = resolve_dispatch_tier(conn, task, lane=lane, cfg=cfg)
    except Exception as exc:
        if is_review:
            from hermes_cli.kanban_review_model import ReviewModelUnavailable
            raise ReviewModelUnavailable(f"Review model resolution failed: {exc}") from exc
        logger.warning("kanban model tier: resolve failed for %s (%s)", getattr(task, "id", "?"), exc,
                       exc_info=True)
        return "error"
    if reason == "pinned" and not _str(getattr(task, "provider_override", None)):
        # A row pinned before pin-time binding (concrete model, NULL provider) used to take its provider from
        # the profile's config.yaml at spawn; bind it with the same resolver the create paths use. [daveey]
        from hermes_cli.kanban_pin_provider import resolve_legacy_pin
        bound, unresolved = resolve_legacy_pin(task)
        if bound:
            task.provider_override = bound
            reason = "pinned+provider_resolved"
        elif unresolved:
            logger.warning("kanban model tier: %s pin has no provider (%s)", getattr(task, "id", "?"), unresolved)
            reason = "pinned+provider_unresolved"
    if model:
        from agent.model_routing import is_class_selector
        why = class_unroutable_reason(model) if is_class_selector(model) else None
        if why:
            if is_review and (implementer_model or reason == "review_override"):
                from hermes_cli.kanban_review_model import ReviewModelUnavailable
                raise ReviewModelUnavailable(f"Review model {model} is unroutable: {why}")
            # A tier-chosen class this host cannot route would crash the worker at startup ("No eligible
            # standard routes under billing/capability policy", 2026-10-27: shared config switched
            # kanban.default_model to class:standard before the host's route registry landed). Keep the
            # profile's own model instead; the next run re-decides once routes exist.
            logger.warning("kanban model tier: %s is not routable on this host (%s); %s keeps its profile model",
                           model, why, getattr(task, "id", "?"))
            reason = f"{reason}+unroutable:{model}:{why}"
            model = provider = None
    origin = (getattr(task, "model_override", None), getattr(task, "provider_override", None))
    repinned = bool(model) and reason.startswith("pinned+pin_quota_reroute")
    if model:
        task.model_override = model
        task.provider_override = provider
    if reason != "off":
        try:
            from hermes_cli import kanban_db as _kb
            payload = {"reason": reason, "model": model or _str(getattr(task, "model_override", None)) or None,
                       "provider": provider or _str(getattr(task, "provider_override", None)) or None,
                       "lane": lane}
            if is_review:
                payload["implementer_model"] = implementer_model
                if reason == "review_override_refused":
                    # The receipt names what the handoff asked for and why this host substituted. [daveey]
                    from hermes_cli.kanban_review_model import refused_override
                    payload["requested"] = refused_override(conn, task)
                    logger.warning("kanban review %s: review_model %s refused by this host's routing registry; "
                                   "reviewing on %s", task.id, (payload["requested"] or {}).get("model"), model)
            with _kb.write_txn(conn):
                _kb._append_event(conn, task.id, "model_tier", payload, run_id=getattr(task, "current_run_id", None))
                if repinned:
                    # The pin moves for this run only (in memory); the reroute is always on the record.
                    from hermes_cli.kanban_pin_reroute import record
                    record(conn, task, (_str(origin[0]), _str(origin[1]).lower()), (_str(model), _str(provider)))
        except Exception as exc:
            if is_review:
                from hermes_cli.kanban_review_model import ReviewModelUnavailable
                raise ReviewModelUnavailable("Cannot persist review model receipt") from exc
            if repinned:
                # Never an unrecorded reroute: the run goes back to its pin (and its wall).
                logger.warning("kanban model tier: pin reroute record failed for %s; keeping the pin (%s)",
                               getattr(task, "id", "?"), exc, exc_info=True)
                task.model_override, task.provider_override = origin
                return "pinned+pin_quota_reroute_unrecorded"
            logger.debug("kanban model tier: event write failed for %s (%s)", getattr(task, "id", "?"), exc,
                         exc_info=True)
    return reason
