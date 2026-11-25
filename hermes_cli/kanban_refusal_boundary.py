"""Refusal exclusions are constraints, never a route offer or permission to reroute."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from agent.model_routing import ModelRoutingError, is_class_selector

_UNSET = object()

class RefusedRouteError(ModelRoutingError):
    """No proven eligible, unrefused execution route; no provider request is permitted."""


def require_route(conn, task, model, provider, *, lane='ready', check_peers=True):
    """Validate the FINAL tuple without choosing a replacement or changing refusal history."""
    from hermes_cli.kanban_refusal_fallback import refused_providers, eligible_routes
    refused = refused_providers(conn, task.id)
    if not refused:
        return
    from hermes_cli.kanban_review_model import is_review_run
    provider = str(provider or '').strip().lower()
    if not model or is_class_selector(model) or provider in ('', 'auto', 'none'):
        raise RefusedRouteError('Safety exclusion requires a concrete execution route')
    if provider in refused:
        raise RefusedRouteError(f'Safety exclusion refuses execution on {provider}')
    if not check_peers or is_review_run(conn, task, lane):
        # Reviewers come from the review resolver, not class peers (as reroute/admission): the refused-provider
        # fence above still applies, only the peer-eligibility check is skipped.
        return tuple(refused)
    # A previously admitted peer can disappear before pace consumption, owner admission or exec.
    # Never fall back to a refused pool (nor silently allocate a different peer) in that case.
    try:
        eligible = eligible_routes(conn, task, lane)
    except Exception as exc:  # health: allow BLE001 -- unknown eligibility never authorizes execution
        raise RefusedRouteError('Safety exclusion eligibility unavailable') from exc
    if (model, provider) not in eligible:
        raise RefusedRouteError('Safety exclusion execution peer is no longer eligible')
    return tuple(refused)


def require_policy(policy, *, model=_UNSET, provider=_UNSET, request=False):
    """Fresh board read at real owner exec and worker construction, including saved policies.

    ``request=True`` (outbound request): the same ended run with no successor claimant may finish its
    reply on an unrefused provider; a live claim also requires a concrete route, not peer availability."""
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
                # Closing reply: only the SAME ended run (including reclaim), with no other claimant,
                # on an unrefused concrete route. Never reopens tools/work or authorizes another claim.
                if (request and task is not None and run is not None
                        and run['claim_lock'] == context['claim_lock'] and run['ended_at'] is not None
                        and run['status'] != 'running' and task.status != 'running'
                        and task.current_run_id in (None, context['run_id'])
                        and task.claim_lock in (None, context['claim_lock'])):
                    selected = str((policy.provider if provider is _UNSET else provider) or '').strip().lower()
                    if selected in ('', 'auto', 'none') or selected in refused_providers(conn, task.id):
                        raise RefusedRouteError(f'Safety exclusion refuses execution on {selected or "unknown"}')
                    return
                raise RefusedRouteError('Safety exclusion execution claim is stale')
            selected_model = policy.model if model is _UNSET else model
            selected_provider = policy.provider if provider is _UNSET else provider
            return require_route(conn, task, selected_model, selected_provider, check_peers=not request,
                                 lane=kb._retry_status_for_run(conn, task.id, task.current_run_id))
    except sqlite3.Error as exc:
        raise RefusedRouteError('Safety exclusion execution history unavailable') from exc
