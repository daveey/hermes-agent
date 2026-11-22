"""Atomic read-before-replace fence for a spend-stop episode's budget decision. [daveey]

Observers may all see a stop; only the first stale reader may change its budget.
A deliberate correction must acknowledge the committed decision's local comment.
The fence survives promotion: a delayed observer must not overwrite the release.
"""
from __future__ import annotations

import json
import shlex

from hermes_cli import kanban_release_comments as comments
from hermes_cli.kanban_sync_divergence import _event_order


def check(conn, task_id, *, seen=None, board=None):
    """Under the budget writer lock; return the latest stop identity, or None.

    Use portable event order and comment UID resolution, never a wire-local id as
    the receiving board's read watermark. Legacy decisions resolve through their
    following system comment. Incomplete evidence refuses rather than grants.
    """
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind IN "
        "('spend_budget_triaged','spend_budget_set','commented')", (task_id,))]
    events.sort(key=_event_order)
    stops = [e for e in events if e['kind'] == 'spend_budget_triaged']
    if not stops:
        return None
    stop = stops[-1]
    episode = stop.get('uid') or str(stop['id'])
    changed = [e for e in events if e['kind'] == 'spend_budget_set' and _event_order(e) > _event_order(stop)]
    if not changed:
        return episode
    last = changed[-1]
    reference = json.loads(last['payload'] or '{}').get('decision_comment')
    notes = [e for e in events if e['kind'] == 'commented' and _event_order(e) > _event_order(last)
             and json.loads(e['payload'] or '{}').get('author') == 'kanban-spend-budget']
    rows = conn.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    note = last if isinstance(reference, dict) else (notes[0] if notes else last)
    payload = reference if isinstance(reference, dict) else json.loads(note['payload'] or '{}')
    resolved = comments._resolve(rows, note, payload) if isinstance(reference, dict) or notes else []
    listing_payload = {**payload, 'writer_session': f"spend decision for episode {episode}"}
    if not resolved:
        unread = [comments._listing(None, 'Budget decision comment unavailable; wait for sync and reread. '
                                    '--seen cannot acknowledge missing evidence.', note, listing_payload)]
    else:
        unread = [comments._listing(r['id'], r['body'], note, listing_payload)
                  for r in resolved if seen is None or r['id'] > seen]
    if unread:
        command = ['hermes', 'kanban']
        if board:
            command += ['--board', board]
        newest = max((r['id'] for r in unread if r['id'] is not None), default=seen or 0)
        command += ['spend-budget', task_id, '--seen', str(newest)]
        raise comments.UnreadComments(unread, shlex.join(command))
    return episode
