"""Current lifecycle episode and overdue attention; never an ask or permission.

Detached timestamps can travel through replicas and annotation snapshots. Only
source acquisition reads events; clock transitions reuse that captured evidence.
"""
import json
import math
import sqlite3

TRIAGE_AFTER_S = 600
_FIXED = {
    'spend_budget_triaged': 'triage', 'spend_budget_held': 'triage', 'decision_retained': 'triage',
    'contained': 'triage', 'gave_up': 'triage', 'supervisor_escalated': 'triage',
    'decision_protected_stopped': 'triage', 'block_loop_detected': 'triage', 'agent_block_escalated': 'triage',
    # kanban_block_policy recheck sweep (blocked -> triage) and kanban_refusal_fallback/kanban_retain's
    # pending-spend-decision safety stop (ready -> triage).
    'ask_rechecked': 'triage', 'decision_hold_restricted': 'triage',
    # kanban_stop_ended's local-shadow administrative stop (ended run -> triage).
    'administrative_stop_restricted': 'triage',
    'claimed': 'running', 'spawned': 'running', 'unblocked': 'ready', 'triage_swept': 'ready',
    'scheduled': 'scheduled', 'continued': 'scheduled', 'blocked': 'blocked',
    'completed': 'done', 'archived': 'archived', 'review_requested': 'review',
    'specified': 'todo', 'decomposed': 'todo', 'dependency_wait': 'todo',
}
_PAYLOAD = {'created', 'status', 'reopened', 'no_progress_escalated',
            # Wakes and reopens record their own landing only in the payload.
            'condition_met', 'continuation_woken', 'review_reopened', 'changes_requested'}
_PROMOTED = {'promoted', 'promoted_manual', 'unblocked'}
# Writers that record their landing in ``payload.status`` (as kanban_sync_divergence._landing reads it): the
# recorded status wins over the kind's default. ``block_task``/``route_block`` land an exhausted budget or a
# workspace-source refusal loop in ``triage`` under a ``blocked`` event; ``continue_task`` records its landing.
_RECORDED = {'blocked', 'continued', 'scheduled', 'block_loop_detected', 'dependency_wait'}
# ``blocked`` events written before block_task recorded a landing: these re-kinds landed ``triage``.
_TRIAGE_REKINDS = frozenset({'continuation_exhausted', 'goal_unachievable'})
_STATUSES = frozenset({'triage', 'todo', 'ready', 'running', 'blocked', 'scheduled', 'review', 'done', 'archived'})
# kanban_transfer.import_board parks dir/worktree cards in triage and records it only as ``parked``.
_IMPORTED = 'imported'
LIFECYCLE_KINDS = tuple(sorted(set(_FIXED) | _PAYLOAD | _PROMOTED | {_IMPORTED}))


def landing(kind, payload):
    """Status one lifecycle event left the card in, else None (not a lifecycle move)."""
    if kind == _IMPORTED:
        return 'triage' if payload.get('parked') is True else None
    recorded = payload.get('status')
    recorded = recorded if recorded in _STATUSES else None
    if kind in _PROMOTED:
        return recorded or 'ready'
    if kind in _PAYLOAD:
        return recorded
    if kind in _RECORDED and recorded:
        return recorded
    if kind == 'blocked' and payload.get('rekind_reason') in _TRIAGE_REKINDS \
            and not payload.get('shared_blocker_probe'):
        return 'triage'
    return _FIXED.get(kind or '')


def _epoch(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def _obj(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def current_entry(row):
    """First event in the current consecutive triage episode, else unknown.

    No creation/update/heartbeat fallback. Repeated holds retain even an unknown
    first timestamp; a later valid hold cannot fabricate the missing entry.
    """
    if row.get('status') != 'triage' or row.get('stale') or row.get('partial_source_unknown'):
        return None
    if 'triage_entered_at' in row:
        return _epoch(row['triage_entered_at'])
    phase, entered = None, None
    for event in row.get('events') or ():
        if not isinstance(event, dict):
            continue
        landed = landing(event.get('kind'), _obj(event.get('payload')))
        if landed is None:
            continue
        if landed == 'triage' and phase != 'triage':
            entered = _epoch(event.get('created_at'))
        elif landed != 'triage':
            entered = None
        phase = landed
    return entered if phase == 'triage' else None


def overdue(row, *, now):
    entered = current_entry(row)
    return entered is not None and entered <= now and now - entered > TRIAGE_AFTER_S


def next_transition(row, *, now):
    entered = current_entry(row)
    if entered is None or entered > now or overdue(row, now=now):
        return None
    return math.nextafter(entered + TRIAGE_AFTER_S, math.inf)


def annotate(conn, rows):
    """Batch capture lifecycle evidence inside the caller's source transaction.

    Only triage cards need histories. Callers retain this detached result under
    their existing source revision memo; elapsed clock passes never call here.
    """
    rows = list(rows)
    targets = {str(row['id']) for row in rows if row.get('status') == 'triage'}
    events = {tid: [] for tid in targets}
    # One statement per call and none without a triage card: hot membership passes call this per status set,
    # so no schema probe. A board without the event columns reads as unknown entry, never a fabricated age.
    if targets:
        try:
            found = conn.execute(
                'SELECT task_id, kind, created_at, payload FROM task_events '
                'WHERE task_id IN (SELECT value FROM json_each(?)) '
                'AND kind IN (SELECT value FROM json_each(?)) ORDER BY id',
                (json.dumps(sorted(targets)), json.dumps(LIFECYCLE_KINDS))).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such' not in str(exc):
                raise
            found = ()
        for tid, kind, at, payload in found:
            events[str(tid)].append({'kind': kind, 'created_at': at, 'payload': payload})
    for row in rows:
        row['triage_entered_at'] = current_entry({'status': row.get('status'), 'events': events.get(str(row['id']), ())})
    return rows


def marker(card):
    entered = current_entry(card)
    if entered is None:
        return set()
    key = str(card.get('key') or card['id'])
    return {(key, 'triage:' + json.dumps({'entered_at': entered}, sort_keys=True), False)}


def entries(markers):
    """Owner-qualified, lineage-deduplicated detached clocks."""
    return {key: json.loads(bucket[len('triage:'):]) for key, bucket, _ in markers if bucket.startswith('triage:')}


def project(row, markers, *, now):
    """Operational red counts, kept separate from human-only needs_you."""
    clocks = entries(markers)
    aged = {key for key, value in clocks.items()
            if overdue({'status': 'triage', 'triage_entered_at': value['entered_at']}, now=now)}
    # Human asks are already red. Count each card exactly once, including lineage aliases.
    aged -= {key for key, bucket, _ in markers if bucket == 'blocked'}
    if aged:
        row['kanban_triage_attention'] = {
            'count': len(aged), 'refs': sorted(aged),
            'parked': len(aged & {key for key, _, parked in markers if parked}),
            'held': len(aged & {key for key, bucket, _ in markers if bucket == 'held'}),
            'stalled': len(aged & {key for key, bucket, _ in markers if bucket == 'stalled'}),
        }
    else:
        row.pop('kanban_triage_attention', None)


def deadlines(markers, *, now):
    return [at for value in entries(markers).values()
            if (at := next_transition({'status': 'triage', 'triage_entered_at': value['entered_at']}, now=now)) is not None]
