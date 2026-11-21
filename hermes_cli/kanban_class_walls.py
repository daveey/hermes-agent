"""Read-only class eligibility against known host-local provider walls.

Concrete choices are per-run allocations, never stored task restrictions.
"""
from agent.model_routing import eligible_routes, is_class_selector, validate_policy
from agent.model_routing_agent import class_requirements
from hermes_cli.config import is_provider_enabled


def unroutable_class(conn, task_id, *, lane):
    """Fail closed per card across the entire non-allocating eligibility read."""
    try:
        return _unroutable_class(conn, task_id, lane=lane)
    except Exception as exc:  # health: allow BLE001 -- no config/resolver fault may abort sibling admission
        import logging
        logging.getLogger(__name__).warning('kanban class eligibility unavailable for %s (%s)',
                                            task_id, type(exc).__name__)
        return 'config_unavailable'


def _unroutable_class(conn, task_id, *, lane):
    """Refuse unusable local policy; review and quota owners retain their typed gates."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_model_neutral import routing_view
    from hermes_cli.kanban_model_tier import class_unroutable_reason, resolve_model_tier

    task = kb.get_task(conn, task_id)
    from hermes_cli.kanban_quota_episode import active
    from hermes_cli.kanban_review_model import is_review_run
    if task is None or active(conn, task_id):
        return None  # the existing episode owns its typed hold and reset deadline
    config = _assignee_config(task.assignee)
    if not isinstance(config, dict):
        return 'config_unavailable'
    providers = config.get('providers', {})
    if not isinstance(providers, dict) or any(not isinstance(block, dict) for block in providers.values()):
        return 'config_unavailable'
    if is_review_run(conn, task, lane):
        # Review owns candidate strength/fallback, provenance and quota walls.
        # A disabled host policy is never permission to use the profile model.
        policy = config.get('model_routing') or {}
        validate_policy(policy)
        return None if policy.get('enabled') else 'routing_disabled'
    model, _, _ = resolve_model_tier(conn, routing_view(task), lane=lane)
    if not is_class_selector(model):
        return None
    return class_unroutable_reason(model, config)


def pickup_refusal(replica, card):
    """Use local class eligibility on replica history before consuming pickup budget."""
    try:
        why = unroutable_class(replica, card['id'], lane=card['status'])
        if why:
            return 'model_unroutable', f'model_unroutable:{why}'
        # The review owner also has structural (not only deadline) refusals.
        # Predict those read-only so a host with no reviewer cannot churn imports.
        from types import SimpleNamespace
        from hermes_cli.kanban_review_admission import hold
        result = SimpleNamespace(clock_held={}, respawn_guarded=[])
        if hold(replica, card['id'], result, lane=card['status'], dry_run=True):
            return 'review_model_unavailable', result.respawn_guarded[-1][1]
        return None
    except Exception as exc:  # health: allow BLE001 -- fail closed for this candidate, never the pickup loop
        import logging
        logging.getLogger(__name__).warning('kanban pickup eligibility unavailable for %s (%s)',
                                            card['id'], type(exc).__name__)
        return 'model_unroutable', 'model_unroutable:config_unavailable'


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
    # Only walls that constrain this class may force a concrete per-run alternate.
    # Unrelated provider state must leave healthy weighted allocation deferred.
    candidate_providers = {route['provider'] for route in candidates}
    known = {provider: until for provider, until in pw.active_walls(now=now).items()
             if provider in candidate_providers}
    for provider, until in (extra_walls or {}).items():
        if provider in candidate_providers:
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
