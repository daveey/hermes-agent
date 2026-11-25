"""Refusal exclusions are constraints, never a route offer or permission to reroute."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from agent.model_routing import ModelRoutingError, is_class_selector

_UNSET = object()

class RefusedRouteError(ModelRoutingError):
    """No proven eligible, unrefused execution route; no provider request is permitted."""


def require_route(conn, task, model, provider, *, lane='ready'):
    """Validate the FINAL tuple without choosing a replacement or changing refusal history."""
    from hermes_cli.kanban_refusal_fallback import refused_providers, eligible_routes
    refused = refused_providers(conn, task.id)
    if not refused:
        return
    provider = str(provider or '').strip().lower()
    if not model or is_class_selector(model) or provider in ('', 'auto', 'none'):
        raise RefusedRouteError('Safety exclusion requires a concrete execution route')
    if provider in refused:
        raise RefusedRouteError(f'Safety exclusion refuses execution on {provider}')
    # A previously admitted peer can disappear before pace consumption, owner admission or exec.
    # Never fall back to a refused pool (nor silently allocate a different peer) in that case.
    try:
        eligible = eligible_routes(conn, task, lane)
    except Exception as exc:  # health: allow BLE001 -- unknown eligibility never authorizes execution
        raise RefusedRouteError('Safety exclusion eligibility unavailable') from exc
    if (model, provider) not in eligible:
        raise RefusedRouteError('Safety exclusion execution peer is no longer eligible')
    return tuple(refused)


def require_policy(policy, *, model=_UNSET, provider=_UNSET):
    """Fresh board read at real owner exec and worker construction, including saved policies."""
    if policy is None or not getattr(policy, 'kanban_json', None):
        return
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_lockguard import connect
    context = json.loads(policy.kanban_json)
    try:
        with closing(connect(Path(context['db']).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            task = kb.get_task(conn, context['task_id'])
            from hermes_cli.kanban_refusal_fallback import refused_providers
            if task is not None and not refused_providers(conn, task.id):
                return  # Ordinary claims keep their existing scope/source/capsule validators.
            run = conn.execute('SELECT status,claim_lock,ended_at FROM task_runs WHERE id=? AND task_id=?',
                               (context['run_id'], context['task_id'])).fetchone()
            if (task is None or task.status != 'running' or task.current_run_id != context['run_id']
                    or task.claim_lock != context['claim_lock'] or run is None
                    or tuple(run) != ('running', context['claim_lock'], None)):
                raise RefusedRouteError('Safety exclusion execution claim is stale')
            selected_model = policy.model if model is _UNSET else model
            selected_provider = policy.provider if provider is _UNSET else provider
            return require_route(conn, task, selected_model, selected_provider,
                                 lane=kb._retry_status_for_run(conn, task.id, task.current_run_id))
    except sqlite3.Error as exc:
        raise RefusedRouteError('Safety exclusion execution history unavailable') from exc
