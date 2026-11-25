"""Shared ordinary semantic-source selection; ordering remains adapter-owned."""
from __future__ import annotations

import json

KINDS = {'todo': ('created', 'linked', 'unlinked', 'dependency_wait', 'unblocked'),
         'ready': ('created', 'promoted_manual', 'unblocked', 'reclaimed'),
         'review': ('review_requested', 'created'), 'blocked': ('blocked',),
         'scheduled': ('scheduled', 'continued'),
         'triage': ('failure_finalized', 'spend_budget_decision_ready', 'created'), 'running': ('claimed',)}


def _data(event):
    try:
        raw = event.get('payload')
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def select(status, events, *, order_key, episode=None):
    """Select one source without treating comments as clocks or releases as grants."""
    def assignment(event):
        if status != 'todo' or event.get('kind') != 'blocked':
            return False
        data = _data(event)
        return data is not None and data.get('kind') == 'assignment' and not any(
            e.get('kind') == 'unblocked' and order_key(e) > order_key(event) for e in events)

    def matches(event):
        if event['kind'] not in KINDS.get(status, ()):
            return assignment(event)
        # An unblock is a todo source only when parents re-gated it there; a release to ready is not.
        return status != 'todo' or event['kind'] != 'unblocked' or (_data(event) or {}).get('status') == 'todo'

    if status == 'running' and not episode:
        # The owning run's stamped dependency handoff (link before park) outranks its claim. A later
        # claim, or a later unstamped edge change, returns the row to the claim source.
        edges = [e for e in events if e['kind'] in ('claimed', 'linked', 'unlinked')]
        last = max(edges, key=order_key) if edges else None
        intent = (_data(last) or {}).get('actionability_intent') if last and last['kind'] != 'claimed' else None
        if isinstance(intent, dict) and intent.get('status') == 'running':
            return last
    candidates = [e for e in events if e.get('uid') == episode] if episode else [e for e in events if matches(e)]
    return max(candidates, key=order_key) if candidates else None
