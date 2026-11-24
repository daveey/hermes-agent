"""Shared ordinary semantic-source selection; ordering remains adapter-owned."""
from __future__ import annotations

import json

KINDS = {'todo': ('created', 'linked', 'dependency_wait'), 'ready': ('created', 'promoted_manual', 'unblocked', 'reclaimed'),
         'review': ('review_requested', 'created'), 'blocked': ('blocked',),
         'scheduled': ('scheduled', 'continued'),
         'triage': ('failure_finalized', 'spend_budget_decision_ready', 'created'), 'running': ('claimed',)}


def select(status, events, *, order_key, episode=None):
    """Select one source without treating comments as clocks or releases as grants."""
    def assignment(event):
        if status != 'todo' or event.get('kind') != 'blocked':
            return False
        try:
            raw = event.get('payload')
            data = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, ValueError):
            return False
        return isinstance(data, dict) and data.get('kind') == 'assignment' and not any(
            e.get('kind') == 'unblocked' and order_key(e) > order_key(event) for e in events)

    candidates = [e for e in events if e.get('uid') == episode] if episode else [
        e for e in events if e['kind'] in KINDS.get(status, ()) or assignment(e)]
    return max(candidates, key=order_key) if candidates else None
