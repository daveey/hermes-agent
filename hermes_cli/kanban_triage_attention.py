"""Current lifecycle episode and overdue attention; never an ask or permission.

Detached timestamps can travel through replicas and annotation snapshots. Only
source acquisition reads events; clock transitions reuse that captured evidence.
"""
import json
import math

TRIAGE_AFTER_S = 600
_FIXED = {
    'spend_budget_triaged': 'triage', 'spend_budget_held': 'triage', 'decision_retained': 'triage',
    'contained': 'triage', 'gave_up': 'triage', 'supervisor_escalated': 'triage',
    'decision_protected_stopped': 'triage', 'block_loop_detected': 'triage', 'agent_block_escalated': 'triage',
    'claimed': 'running', 'spawned': 'running', 'unblocked': 'ready', 'triage_swept': 'ready',
    'scheduled': 'scheduled', 'continued': 'scheduled', 'blocked': 'blocked',
    'completed': 'done', 'archived': 'archived', 'review_requested': 'review',
    'specified': 'todo', 'decomposed': 'todo',
}
_PAYLOAD = {'created', 'status', 'reopened', 'no_progress_escalated'}
_PROMOTED = {'promoted', 'promoted_manual', 'unblocked'}
LIFECYCLE_KINDS = tuple(sorted(set(_FIXED) | _PAYLOAD | _PROMOTED))


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
        kind, payload = event.get('kind'), _obj(event.get('payload'))
        landing = (payload.get('status', 'ready') if kind in _PROMOTED else
                   payload.get('status') if kind in _PAYLOAD else _FIXED.get(kind or ''))
        if landing is None:
            continue
        if landing == 'triage' and phase != 'triage':
            entered = _epoch(event.get('created_at'))
        elif landing != 'triage':
            entered = None
        phase = landing
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
    from hermes_cli.kanban_table_columns import table_columns
    rows = list(rows)
    targets = {str(row['id']) for row in rows if row.get('status') == 'triage'}
    columns = table_columns(conn, 'task_events')
    events = {tid: [] for tid in targets}
    if targets and {'task_id', 'id', 'kind', 'created_at', 'payload'} <= columns:
        for item in conn.execute(
                'SELECT task_id, kind, created_at, payload FROM task_events '
                'WHERE task_id IN (SELECT value FROM json_each(?)) '
                'AND kind IN (SELECT value FROM json_each(?)) ORDER BY id',
                (json.dumps(sorted(targets)), json.dumps(LIFECYCLE_KINDS))):
            tid, kind, at, payload = item
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
