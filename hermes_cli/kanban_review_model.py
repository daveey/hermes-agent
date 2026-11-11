"""Review-only model selection; implementation pins are not reviewer pins. [daveey]"""
from __future__ import annotations

import json

from agent.model_routing import ModelRoutingError, eligible_routes, is_class_selector, validate_policy


class ReviewModelUnavailable(ModelRoutingError):
    """No independent reviewer is configured; do not spawn the implementer's model."""


def _override(conn, task):
    # Metadata belongs to the handoff run, not to the Task row. Read only the
    # latest handoff so a subsequent review can deliberately remove an override.
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id=? AND outcome='review_requested' "
        "ORDER BY id DESC LIMIT 1", (task.id,),
    ).fetchone()
    metadata = json.loads(row[0]) if row and row[0] else {}
    if not isinstance(metadata, dict):
        raise ReviewModelUnavailable("Review handoff metadata must be a mapping")
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
    if is_class_selector(model) and provider != "auto":
        raise ReviewModelUnavailable("A review class requires provider auto")
    return model, provider, "review_override"


def _distinct_deep(pin, assignee):
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
        if route["model"] != pin:
            return route["model"], route["provider"], "review_unpinned"
    raise ReviewModelUnavailable(f"No eligible deep review model distinct from {pin!r}")


def resolve_review_model(conn, task, cfg):
    """Return a review override/distinct route, or None for legacy unpinned reviews."""
    override = _override(conn, task)
    if override is not None:
        return override
    pin = str(getattr(task, "model_override", None) or "").strip()
    if not pin:
        return None
    if is_class_selector(pin):
        raise ReviewModelUnavailable("A class pin does not identify the implementer; set review_model in the handoff")
    strong = str(cfg.get("strong_model") or "").strip()
    if strong and strong != pin and not is_class_selector(strong):
        from hermes_cli.kanban_model_tier import infer_provider
        return strong, str(cfg.get("strong_provider") or "").strip() or infer_provider(strong), "review_unpinned"
    return _distinct_deep(pin, getattr(task, "assignee", None))
