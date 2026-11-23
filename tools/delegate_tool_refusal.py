"""A subagent stopped by a provider safety refusal is re-run unchanged on the class's other providers. [daveey]

Same rule as a Kanban card (``hermes_cli.kanban_refusal_fallback``): when a child ends ``content_policy_blocked``,
the same goal/context runs once on each other eligible provider of the refusing model's class (enabled, serving on
this host's live pool, not walled), stopping at the first that does not refuse. Only when every one refused does
the original failure return, with ``refused_providers`` listed. Nothing in the goal is reworded and no note about
the refusal is added.

Limits: a Kanban-origin parent routes children by class only (``kanban_delegation_policy``), so it is not
retried here (its card is retried by the Board path); peers come from the ``model_routing`` class policy, so a
child whose model sits in no single class has none; each retry's progress events are the new child's own.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

REFUSED = "content_policy_blocked"


def refused(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("failure_reason") == REFUSED


def _provider(child: Any) -> str:
    return str(getattr(child, "provider", None) or "").strip().lower()


def peers(child: Any, parent_agent: Any, skip: list[str]) -> list[tuple[str, str]]:
    """Unrefused eligible ``(model, provider)`` peers of the child's class, registry order."""
    from hermes_cli.kanban_refusal_fallback import class_peers
    model = str(getattr(child, "model", None) or "")
    task = SimpleNamespace(assignee=getattr(parent_agent, "_profile_name", None))
    try:
        return [(m, p) for m, p in class_peers(task, model) if p not in skip]
    except Exception:  # health: allow BLE001 -- unreadable class policy means no proven peer: return the failure
        logger.warning("delegate refusal fallback: class peers unreadable", exc_info=True)
        return []


def rerun(batch: Any, index: int, task: dict, child: Any, entry: dict, run_child) -> dict:
    """``entry`` or the first non-refused rerun of the same task on another provider (see module doc)."""
    from agent import kanban_delegation_policy as policy
    from tools.delegate_tool import DEFAULT_MAX_ITERATIONS, _build_children, _load_config
    from tools.delegate_tool_routes import route_for_config
    parent = batch.parent_agent
    if policy.origin_for(parent) is not None:
        return entry
    tried = [p for p in (_provider(child),) if p]
    schema = getattr(child, "_delegate_output_schema", None)
    images = getattr(child, "_delegate_images", None)
    for model, provider in peers(child, parent, tried):
        tried.append(provider)
        try:
            route = route_for_config({"model": model, "provider": provider}, parent)
            built, err = _build_children(
                [task], [schema], [route], top_role=batch.top_role, max_iterations=_load_config().get("max_iterations", DEFAULT_MAX_ITERATIONS),
                parent_agent=parent, live_deleg_id=None, live_writers=[], task_images=[images])
        except (ValueError, RuntimeError) as exc:
            logger.warning("delegate refusal fallback: cannot build a child on %s (%s)", provider, exc)
            continue
        if err or not built:
            continue
        again = run_child(index, task, built[0][2])
        if not refused(again):
            again["refusal_fallback"] = {"refused_providers": tried[:-1], "provider": provider}
            return again
    if len(tried) > 1:
        entry["refused_providers"] = tried
    return entry
