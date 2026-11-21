"""Read-only class eligibility against known host-local provider walls.

Concrete choices are per-run allocations, never stored task restrictions.
"""
from agent.model_routing import eligible_routes, is_class_selector, validate_policy
from agent.model_routing_agent import class_requirements
from hermes_cli.config import is_provider_enabled


def unroutable_class(conn, task_id, *, lane):
    """Refuse a proven unusable class before claiming, never fall back to a profile pin."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_model_neutral import routing_view
    from hermes_cli.kanban_model_tier import class_unroutable_reason, resolve_model_tier

    task = kb.get_task(conn, task_id)
    from hermes_cli.kanban_quota_episode import active
    from hermes_cli.kanban_review_model import is_review_run
    if task is None or active(conn, task_id):
        return None  # the existing episode owns its typed hold and reset deadline
    if is_review_run(conn, task, lane):
        return None  # review owns provenance refusal, bounded escalation and route eligibility
    model, _, _ = resolve_model_tier(conn, routing_view(task), lane=lane)
    if not is_class_selector(model):
        return None
    try:
        config = _assignee_config(task.assignee)
    except ImportError:
        return 'config_unavailable'  # missing profile machinery is unknown eligibility, not a tick-wide fault
    if config is None:
        return 'config_unavailable'
    return class_unroutable_reason(model, config)


def class_wall_routes(task, model, *, now, extra_walls=None):
    """Return (eligible unwalled routes, observed walls) for an assignee class.

    Policy and capability failures propagate: callers must not guess capacity.
    No quota is allocated and no credential/reserve gate is bypassed.
    """
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_model_assignment import _assignee_config
    if not is_class_selector(model):
        return [], {}
    config = _assignee_config(task.assignee) or {}
    policy = config.get('model_routing') or {}
    validate_policy(policy)
    if not policy.get('enabled'):
        return [], {}
    providers = config.get('providers') or {}
    candidates = eligible_routes(policy, model.removeprefix('class:'), class_requirements(config),
                                 eligible=lambda r: is_provider_enabled(providers.get(r['provider'], {})))
    known = dict(pw.active_walls(now=now))
    for provider, until in (extra_walls or {}).items():
        known[provider] = max(known.get(provider, 0), until)
    provider_walls = dict(known)
    routes = []
    for route in candidates:
        provider = route['provider']
        until = pw.live_quota_wall(provider, route['model'])
        if until is not None:
            # Observed deadlines inform holds/reroute receipts, not sibling eligibility.
            known[provider] = max(known.get(provider, 0), until)
        if provider not in provider_walls and until is None:
            routes.append(route)
    return routes, known
