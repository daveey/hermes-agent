"""Classify migration churn without relaxing ownership or value fences."""
from __future__ import annotations

from hermes_cli.kanban_priority import PRIORITY_CAP


def classify(planned: list[dict], observed: list[dict], *, writer: str | None = None) -> tuple[list[dict], dict]:
    """Require positive terminal evidence; missing rows are not departures.

    The caller must hold its write transaction when using the returned active rows.
    Foreign captured rows present locally are checked for owner drift too, so an
    incoming transfer cannot masquerade as a newly created capped card.
    """
    indexed = {row['id']: row for row in observed}
    recorded = {row['id']: row for row in planned}
    active, skipped, arrivals = [], [], []
    for row in planned:
        current = indexed.get(row['id'])
        if writer is not None and row['origin'] != writer and current is None:
            continue
        if current is None or current['created_on'] != row['origin']:
            raise ValueError('ownership or membership changed; capture a new manifest')
        if writer is not None and row['origin'] != writer:
            continue
        if current['status'] in ('done', 'archived'):
            skipped.append({'id': row['id'], 'status': current['status'], 'priority': current['priority']})
        else:
            active.append(row)
    for current in observed:
        if current['id'] in recorded or current['status'] in ('done', 'archived'):
            continue
        if writer is not None and current['created_on'] != writer:
            continue
        if not current['created_on'] or type(current['priority']) is not int or current['priority'] > PRIORITY_CAP:
            raise ValueError('ownership or membership changed; new open card exceeds cap or lacks origin')
        arrivals.append({'id': current['id'], 'origin': current['created_on'], 'priority': current['priority']})
    report = {}
    if skipped:
        report['skipped'] = sorted(skipped, key=lambda row: row['id'])
    if arrivals:
        report['arrivals'] = sorted(arrivals, key=lambda row: row['id'])
    return active, report
