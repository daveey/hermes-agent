"""Review-only model selection; implementation pins are not reviewer pins. [daveey]"""
from __future__ import annotations

import json
from typing import Any

from agent.model_routing import ModelRoutingError, eligible_routes, is_class_selector, validate_policy


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value


# model_tier reason when this host's routing registry refuses the handoff's review_model: the review runs on a
# registered model distinct from the implementer instead of spawning a worker that dies at startup.
OVERRIDE_REFUSED = "review_override_refused"


class ReviewModelUnavailable(ModelRoutingError):
    """No independent reviewer is configured; do not spawn the implementer's model."""


def _handoff(conn, task):
    """``(run_id, metadata)`` of the latest review handoff, or ``(None, {})``."""
    # Metadata belongs to the handoff run, not to the Task row. Read only the
    # latest handoff so a subsequent review can deliberately remove an override.
    row = conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id=? AND outcome='review_requested' "
        "ORDER BY id DESC LIMIT 1", (task.id,),
    ).fetchone()
    metadata = json.loads(row[1]) if row and row[1] else {}
    if not isinstance(metadata, dict):
        raise ReviewModelUnavailable("Review handoff metadata must be a mapping")
    return (row[0] if row else None), metadata


def _override(conn, task):
    return validate_review_override(_handoff(conn, task)[1])


def validate_review_override(metadata):
    """Shared ingress/dispatch contract, including legacy automatic class routing."""
    model = metadata.get("review_model")
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ReviewModelUnavailable("review_model must be a nonempty model identifier")
    from hermes_cli.kanban_model_tier import infer_provider
    provider = metadata.get("review_provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ReviewModelUnavailable("review_provider must be a nonempty provider identifier")
    model = model.strip()
    provider = (provider or ("auto" if is_class_selector(model) else infer_provider(model)))
    if is_class_selector(model):
        if provider not in ("auto", "board-default"):
            raise ReviewModelUnavailable("A review class requires provider auto")
        provider = "auto"  # Historical handoffs used the board-default sentinel.
    return model, provider, "review_override"


def registry_refusal(model, provider, assignee):
    """The worker's startup pin check (managed routing registry), run before spawn. [daveey]

    Returns the ``ModelPinNotInRegistryError`` a worker for *assignee* on this host would die of, else None."""
    from hermes_cli.kanban_model_assignment import ModelPinNotInRegistryError, _assignee_config, _check_managed_pin
    try:
        _check_managed_pin(model, (provider or "").strip() or None, _assignee_config(assignee))
    except ModelPinNotInRegistryError as exc:
        return exc
    return None


def refused_override(conn, task):
    """``{model, provider, code, allowed}`` when this host refuses the handoff's review_model, else None."""
    override = _override(conn, task)
    if override is None:
        return None
    refusal = registry_refusal(override[0], override[1], getattr(task, "assignee", None))
    if refusal is None:
        return None
    return {"model": override[0], "provider": override[1], "code": refusal.code, "allowed": refusal.allowed}


def _run_model(conn, task_id, run_id, kind, key):
    """Concrete model named by *run_id*'s latest *kind* event under *key*, else None."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? "
                       "ORDER BY id DESC LIMIT 1", (task_id, run_id, kind)).fetchone()
    try:
        payload = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        payload = {}
    model = str(payload.get(key) or "").strip() if isinstance(payload, dict) else ""
    return model if model and not is_class_selector(model) else None


def _implementer_model(conn, task):
    """Concrete model the implementer ran, most direct evidence first. [daveey]

    The handoff run's ``worker_result.route_model`` (the live agent's model: the only concrete name when the
    run was class-routed, e.g. ``kanban.strong_model: class:deep``), then its ``run_route`` receipt (under
    ``pinned+pin_quota_reroute`` that is the peer that ran, not the pin), and only then the card's concrete pin.
    Preferring the pin let ``_distinct_deep(pin)`` pick the rerouted implementer as its own reviewer."""
    run_id = _handoff(conn, task)[0]
    if run_id is not None:
        for kind, key in (("worker_result", "route_model"), ("run_route", "model")):
            model = _run_model(conn, task.id, run_id, kind, key)
            if model:
                return model
    pin = str(getattr(task, "model_override", None) or "").strip()
    return pin if pin and not is_class_selector(pin) else None


# Identical review-admission refusals in a row (spaced by ``infrastructure_cooldown``) before the card leaves
# the silent infrastructure-retry loop for a visible block. [daveey]
DEFERRAL_LIMIT = 3


def deferral_exhausted(conn, task_id, error):
    """True when *error* would be the ``DEFERRAL_LIMIT``-th identical ReviewModelUnavailable deferral in a row.

    Such a refusal is ``infrastructure`` (failures stay 0, the breaker never trips), so a cause that never
    clears (no concrete implementer receipt, no registered independent reviewer) deferred the card forever."""
    import sqlite3
    try:
        rows = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
                            "ORDER BY id DESC LIMIT ?", (task_id, DEFERRAL_LIMIT - 1)).fetchall()
    except sqlite3.Error:
        return False  # unreadable history keeps the plain infrastructure deferral
    return len(rows) == DEFERRAL_LIMIT - 1 and all(
        row[0] == "spawn_failed" and (row[1] or "") == str(error)[:500] for row in rows)


def admission_refusal(conn, task_id, exc):
    """``(infrastructure, force_trip, gave_up extra)`` for a spawn that failed before any worker ran. [daveey]

    A host that cannot place the worker (no restart-safe scope) or an unavailable independent reviewer is
    infrastructure: retried with the budget untouched, until ``deferral_exhausted`` sends the review to a block."""
    from tools.process_registry import RestartSafeScopeUnavailable
    if isinstance(exc, ReviewModelUnavailable) and deferral_exhausted(conn, task_id, str(exc)):
        return False, True, {"admission_refusal": "review_model_unavailable", "deferrals": DEFERRAL_LIMIT}
    from hermes_cli.kanban_pace_reroute import OfferLost  # a lapsed pace move: requeue for a fresh judgment
    return isinstance(exc, (RestartSafeScopeUnavailable, ReviewModelUnavailable, OfferLost)), False, None


def _distinct_deep(pin, assignee, *, registered=False):
    from agent.model_routing_agent import class_requirements
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.config import is_provider_enabled
    config = _assignee_config(assignee) or {}
    policy = config.get("model_routing") or {}
    validate_policy(policy)
    providers = config.get("providers") or {}
    routes = eligible_routes(policy, "deep", class_requirements(config), eligible=lambda route: is_provider_enabled(
        providers.get(route["provider"], {}))) if policy.get("enabled", False) else []
    for route in routes:
        if route["model"] != pin and not (registered and registry_refusal(route["model"], route["provider"], assignee)):
            return route["model"], route["provider"], "review_unpinned"
    raise ReviewModelUnavailable(f"No eligible deep review model distinct from {pin!r}")


def _independent(pin, cfg, assignee, *, registered=False):
    """A reviewer distinct from *pin*: the strong tier, else another deep route. ``registered`` also requires
    the host's routing registry to accept it (the fallback for a refused override)."""
    strong = str(cfg.get("strong_model") or "").strip()
    if strong and strong != pin and not is_class_selector(strong):
        from hermes_cli.kanban_model_tier import infer_provider
        route = strong, str(cfg.get("strong_provider") or "").strip() or infer_provider(strong), "review_unpinned"
        if not (registered and registry_refusal(route[0], route[1], assignee)):
            return route
    return _distinct_deep(pin, assignee, registered=registered)


def independent_routes(pin, cfg, assignee):
    """Every ``(model, provider)`` :func:`_independent` may pick for an implementer on *pin*, in its order:
    the concrete strong tier, then each eligible deep route. Read-only; an unreadable policy yields none."""
    strong = str(cfg.get("strong_model") or "").strip()
    if strong and strong != pin and not is_class_selector(strong):
        from hermes_cli.kanban_model_tier import infer_provider
        yield strong, str(cfg.get("strong_provider") or "").strip() or infer_provider(strong)
    from agent.model_routing_agent import class_requirements
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.kanban_model_assignment import _assignee_config
    config = _assignee_config(assignee) or {}
    policy = config.get("model_routing") or {}
    try:
        validate_policy(policy)
    except ModelRoutingError:  # no provable alternate; the caller keeps its route
        return
    if not policy.get("enabled", False):
        return
    providers = config.get("providers") or {}
    for route in eligible_routes(policy, "deep", class_requirements(config), eligible=lambda r: is_provider_enabled(
            providers.get(r["provider"], {}))):
        if route["model"] != pin:
            yield route["model"], route["provider"]


def resolve_review_model(conn, task, cfg):
    """Return a review override/distinct route, or None for legacy unpinned reviews."""
    override = _override(conn, task)
    assignee = getattr(task, "assignee", None)
    if override is not None:
        refusal = registry_refusal(override[0], override[1], assignee)
        if refusal is None:
            return override
        # A review_model this host's registry refuses (handoff metadata naming a model only another host
        # serves: claude-fable-5-2 on pool workers, 2026-11-12) crashed every review worker at startup until
        # the card gave up. Review on a registered model independent of the implementer instead.
        implementer = _implementer_model(conn, task)
        if not implementer:
            raise ReviewModelUnavailable(
                f"review_model {override[0]!r} is refused here ({refusal.code}) and the implementer's model is "
                "unknown, so no independent registered reviewer can be chosen")
        try:
            model, provider, _ = _independent(implementer, cfg, assignee, registered=True)
        except ReviewModelUnavailable as exc:
            raise ReviewModelUnavailable(f"review_model {override[0]!r} is refused here ({refusal.code}); {exc}") from exc
        return model, provider, OVERRIDE_REFUSED
    pin = str(getattr(task, "model_override", None) or "").strip()
    if not pin:
        return None
    implementer = _implementer_model(conn, task)
    if not implementer:
        raise ReviewModelUnavailable("A class pin does not identify the implementer; set review_model in the handoff")
    return _independent(implementer, cfg, assignee)
