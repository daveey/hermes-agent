"""Live usage probe for a typed subscription quota wall. [daveey]

2026-11-12: a revoked-401 window typed every worker's anthropic pool as quota-walled until 11-17, and
cards with no class alternate sat in ``respawn_guarded subscription_quota_reset`` although three
accounts had weekly headroom. The only way out was hand SQL on the ``quota_episode`` payload.

``live_verdict`` reads every pooled OAuth account's live usage windows (``agent.account_usage``, the
``/api/oauth/usage`` read for anthropic, read-only for Codex). Two callers:

- ``contradicted``: a start-time ``SubscriptionQuotaUnavailable`` is checked by the bounded live
  completion recovery helper, not usage alone. Managed workers retry construction once after a clear.
- ``hermes kanban quota-release <id> --probe`` (``kanban_quota_episode_release``): the operator release
  refuses, with an audited ``quota_release_refused`` event, unless every walled provider shows headroom.
"""
from __future__ import annotations

from collections import namedtuple

PROBE_DEADLINE_SECONDS = 25.0
Verdict = namedtuple('Verdict', 'state detail')  # state: headroom | walled | unknown


def _family(model):
    name = str(model or '').lower()
    return next((family for family in ('opus', 'sonnet') if family in name), None)


def _headroom(snapshot, model):
    """True/False for one account's usage snapshot, None when it could not be read."""
    if snapshot is None or not snapshot.available:
        return None
    family = _family(model)
    for window in snapshot.windows:
        used = window.used_percent
        if used is None or used < 100:
            continue
        if window.scope != 'model' or (family and str(window.label).lower().startswith(family)):
            return False
    return True


def _describe(snapshot):
    if snapshot is None or not snapshot.available:
        return 'unreadable'
    return ', '.join(f'{w.label} {w.used_percent:.0f}%' for w in snapshot.windows if w.used_percent is not None)


def usage_verdict(snapshots, model, labels=None):
    """Headroom when any account can serve *model*; walled only when every account was read and is capped."""
    states = [_headroom(snapshot, model) for snapshot in snapshots]
    labels = labels or [f'account{n}' for n in range(1, len(snapshots) + 1)]
    detail = '; '.join(f'{label}: {_describe(snapshot)}' for label, snapshot in zip(labels, snapshots))
    if any(state is True for state in states):
        return Verdict('headroom', detail)
    if states and all(state is False for state in states):
        return Verdict('walled', detail)
    return Verdict('unknown', detail or 'no pooled OAuth account')


def live_verdict(provider, model):
    """Read every live pooled OAuth account's usage windows (bounded, read-only; tokens never leave here)."""
    from concurrent.futures import ThreadPoolExecutor, wait

    from agent.account_usage import fetch_account_usage
    from agent.credential_pool import STATUS_DEAD, load_pool
    if provider not in ('anthropic', 'openai-codex'):
        return Verdict('unknown', f'no usage endpoint for {provider}')
    entries = [entry for entry in load_pool(provider, allowed_auth_types={'oauth'}).entries()
               if entry.auth_type == 'oauth' and entry.last_status != STATUS_DEAD and entry.runtime_api_key]
    pool = ThreadPoolExecutor(max_workers=max(1, len(entries)))
    try:
        futures = [pool.submit(fetch_account_usage, provider, api_key=entry.runtime_api_key, read_only=True)
                   for entry in entries]
        wait(futures, timeout=PROBE_DEADLINE_SECONDS)
        snapshots = [f.result() if f.done() and not f.exception() else None for f in futures]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return usage_verdict(snapshots, model, [entry.label or entry.id[:8] for entry in entries])


def probe_model(conn, task_id, state):
    """The model the episode's run was routed to (its receipt), else the episode's recorded model."""
    from hermes_cli.kanban_quota_receipt import _owned_receipt
    owned = _owned_receipt(conn, task_id, state['run_id'])
    model = state.get('wall_model') or (owned[0].get('route_model') if owned else None)
    return model if isinstance(model, str) and model else state.get('model')


def verdicts(walls, model, verdict=None):
    """``provider -> Verdict._asdict()`` for every provider *walls* name (``kanban_quota_episode._providers``)."""
    from hermes_cli.kanban_quota_episode import _providers
    verdict = verdict or live_verdict
    targets = sorted({(provider, wall.get('wall_model') or model) for wall in walls for provider in _providers(wall)},
                     key=lambda target: (target[0], str(target[1])))
    found = {}
    for provider, candidate in targets:
        value = verdict(provider, candidate)._asdict()
        # One serving model cannot release another model's confirmed/unknown wall.
        if provider not in found or found[provider]['state'] == 'headroom' or value['state'] == 'walled':
            found[provider] = value
    return found


def contradicted(exc, *, verdict=None):
    """Start-time quota recovery; usage-only injection is retained for read-only verdict callers.

    Default recovery requires a successful completion and a persisted compare-and-swap clear.
    A managed startup already attempted recovery before its single retry; never probe it twice.
    Operator ``live_verdict`` remains read-only and separate from this mutation path.
    """
    from agent.model_routing_unavailable import (
        QuotaContradicted, SubscriptionAuthUnavailable, SubscriptionQuotaUnavailable)
    wall, seen = exc, set()
    while wall is not None and id(wall) not in seen and not isinstance(wall, SubscriptionQuotaUnavailable):
        seen.add(id(wall))
        wall = wall.__cause__ or wall.__context__
    if wall is None or isinstance(wall, SubscriptionAuthUnavailable) or not isinstance(wall, SubscriptionQuotaUnavailable):
        return exc
    if getattr(exc, '_kanban_quota_probe_attempted', False):
        return exc
    if verdict is None:
        from hermes_cli.kanban_quota_recovery import recover_exception
        return (QuotaContradicted(wall.provider, wall.model, 'live completion verified; stale bench cleared')
                if recover_exception(exc) else exc)
    try:
        found = verdict(wall.provider, wall.model)
    except Exception:  # health: allow BLE001 -- a failed probe keeps the typed refusal (fail closed) [daveey]
        return exc
    if found.state != 'headroom':
        return exc
    return QuotaContradicted(wall.provider, wall.model, found.detail)
