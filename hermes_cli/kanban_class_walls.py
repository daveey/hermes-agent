"""Read-only class eligibility against known host-local provider walls.

Concrete choices are per-run allocations, never stored task restrictions.
"""
from agent.model_routing import eligible_routes, is_class_selector, validate_policy
from agent.model_routing_agent import class_requirements
from hermes_cli.config import is_provider_enabled
from hermes_cli.kanban_model_assignment import _assignee_config


def class_wall_routes(task, model, *, now, extra_walls=None):
    """Return (eligible unwalled routes, observed walls) for an assignee class.

    Policy and capability failures propagate: callers must not guess capacity.
    No quota is allocated and no credential/reserve gate is bypassed.
    """
    from hermes_cli import kanban_provider_walls as pw
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
    routes = []
    for route in candidates:
        provider = route['provider']
        until = pw.live_quota_wall(provider, route['model'])
        if until is not None:
            known[provider] = max(known.get(provider, 0), until)
        if provider not in known:
            routes.append(route)
    # A provider's later model probe may have revealed a provider-wide wall.
    return [r for r in routes if r['provider'] not in known], known
