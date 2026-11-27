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


class StopDecisionRequired(ValueError):
    """A standalone limit write cannot settle a restricted-history stop."""


def require_decision(conn, task_id):
    """Inside the budget writer: release owns its composition; standalone CLI edits do not. Only while the card
    is held where release can record that decision (its ``_HELD`` lanes): a running, todo or ready card whose
    Core history still reads a stop as pending has no release route, and refusing there would strand it.
    Only once a renewal covers the restriction: an unrenewed restricted stop keeps its ordinary limit edits
    (cumulative window, promotion still refused), so nothing there reads as a release."""
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_release import _HELD
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (row is not None and row[0] in _HELD and decision.pending(conn, task_id)
            and decision.restricted_floor(conn, task_id) is not None
            and not decision.execution_restricted(conn, task_id)):
        raise StopDecisionRequired(
            f"stop_decision_required: {task_id} still needs a stop decision; no budget was changed. "
            f"Use `hermes kanban release {task_id}` from the owning chat or a human terminal "
            "to validate and record the decision. Its --runs specifies additional runs, not an absolute limit.")


def check(conn, task_id, *, seen=None, board=None):
    """Under the budget writer lock; return the latest stop identity, or None.

    Use portable event order and comment UID resolution, never a wire-local id as
    the receiving board's read watermark. Legacy decisions resolve through their
    following system comment, or their same-second immediate predecessor.
    Incomplete evidence refuses rather than grants.
    """
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM task_events WHERE task_id=?", (task_id,))]
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
    decision = json.loads(last['payload'] or '{}')
    reference = decision.get('decision_comment')
    # Older automatic grants used the reviewer's author, not the manual budget
    # writer. Only that recorded producer may select the reviewer fallback.
    author = ('kanban-spend-reviewer' if decision.get('by') == 'kanban-spend-reviewer'
              else 'kanban-spend-budget')
    notes = [e for e in events if e['kind'] == 'commented' and _event_order(e) > _event_order(last)
             and json.loads(e['payload'] or '{}').get('author') == author]
    if (not notes and reference is None and 'episode' not in decision
            and author == 'kanban-spend-reviewer'):
        # Reviewer grants retain their legacy immediate-predecessor contract.
        # Manual budgets must pass _prior_comment's exact-content/uniqueness guards.
        # Inspect ALL task events: filtering kinds would hide an intervening event.
        index = events.index(last)
        previous = events[index - 1] if index else None
        if (previous and previous['kind'] == 'commented'
                and previous['created_at'] == last['created_at']
                and json.loads(previous['payload'] or '{}').get('author') == author):
            notes = [previous]
    rows = conn.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    note = last if isinstance(reference, dict) else (notes[0] if notes else last)
    payload = reference if isinstance(reference, dict) else json.loads(note['payload'] or '{}')
    resolved = comments._resolve(rows, note, payload) if isinstance(reference, dict) or notes else []
    if not resolved and reference is None and not notes:
        prior = _prior_comment(events, rows, stop, last, decision, author)
        if prior is not None:
            note, payload, resolved = prior
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
        command += ['spend-budget', task_id]
        if resolved:
            newest = max(r['id'] for r in resolved)
            command += ['--seen', str(newest)]
        raise comments.UnreadComments(unread, shlex.join(command))
    return episode


def _prior_comment(events, rows, stop, last, decision, author):
    """Recover only an unambiguous no-visible-stop writer's same-second system comment.

    Explicit references and episode-bearing decisions never fall back. The legacy writer emitted the
    comment first, but attached its UID only when a stop was already visible. Restoring that stop must
    still require the actual local comment's read watermark, not a guessed foreign integer.
    """
    from hermes_cli.kanban_spend_budget import AUTHOR, _override_text

    if 'episode' in decision or author != AUTHOR or not decision.get('by') or not decision.get('reason'):
        return None
    prefix = (f"Spend budget for this card: {_override_text(decision)}, "
              f"set by {decision['by']}: {decision['reason']}. ")
    candidates = [e for e in events if e['kind'] == 'commented'
                  and e['created_at'] == last['created_at']
                  and _event_order(stop) < _event_order(e) < _event_order(last)
                  and json.loads(e['payload'] or '{}').get('author') == AUTHOR]
    if len(candidates) != 1:
        return None
    note = candidates[0]
    payload = json.loads(note['payload'] or '{}')
    resolved = comments._resolve(rows, note, payload)
    if len(resolved) != 1 or not str(resolved[0]['body']).startswith(prefix):
        return None
    return note, payload, resolved
