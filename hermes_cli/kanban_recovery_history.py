"""Recovery-only causal evidence, authored once and transported unchanged by sync.

SQLite IDs locate rows, UUIDs identify them; neither is causal order. A lifecycle
write records the heads it observed. Creation records the ancestor parks it saw.
Restoring either row therefore cannot renew authority. Legacy synced histories
without a unique evidenced head fail closed; unsynced boards retain local order.
"""
from __future__ import annotations

import uuid

META = '_recovery_history_v1'


def synced(conn):
    # A detached board can still contain replayed history. Schema presence, not
    # today's sync toggle, determines whether insertion order is trustworthy.
    return any(r[1] == 'uid' for r in conn.execute('PRAGMA table_info(task_events)'))


def metadata(row):
    from hermes_cli.kanban_resume_exec import _payload
    value = _payload(row['payload']).get(META)
    return value if isinstance(value, dict) else {}


def identity(row):
    if row is None:
        return None
    return metadata(row).get('identity') or row['uid']


def events(conn, target, kinds):
    marks = ','.join('?' for _ in kinds)
    return conn.execute(f'SELECT * FROM task_events WHERE task_id=? AND kind IN ({marks})',
                        (target, *kinds)).fetchall()


def heads(rows):
    seen = {key for row in rows for key in metadata(row).get('predecessors', [])}
    return [row for row in rows if identity(row) not in seen]


def head(conn, target):
    """The unique evidenced lifecycle head of ``target``, or None (legacy or forked history)."""
    # Include intermediate lifecycle events when finding a blocked head: filtering
    # first would discard the links that supersede an older park.
    from hermes_cli.kanban_resume_exec import _LIFECYCLE_KINDS
    tips = heads(events(conn, target, _LIFECYCLE_KINDS))
    return tips[0] if len(tips) == 1 and metadata(tips[0]) else None


def latest(conn, target, kinds):
    if not synced(conn):
        return max(events(conn, target, kinds), key=lambda r: r['id'], default=None)
    tip = head(conn, target)
    return tip if tip is not None and tip['kind'] in kinds else None


def nearest(conn, target, kinds):
    """``(decided, row)``: the causally newest ``kinds`` event at or behind the unique head.

    Undecided (False, None) for legacy or forked history, or when the walk meets an
    unevidenced ancestor or two candidates at once; callers then keep local order.
    """
    from hermes_cli.kanban_resume_exec import _LIFECYCLE_KINDS
    tip = head(conn, target)
    if tip is None:
        return False, None
    by_id = {identity(r): r for r in events(conn, target, _LIFECYCLE_KINDS)}
    frontier, seen = [tip], set()
    while frontier:
        found = [r for r in frontier if r['kind'] in kinds]
        if found:
            return (True, found[0]) if len(found) == 1 else (False, None)
        if any(not metadata(r) for r in frontier):
            return False, None
        seen.update(identity(r) for r in frontier)
        keys = {k for r in frontier for k in metadata(r).get('predecessors', []) if k not in seen}
        if any(k not in by_id for k in keys):
            return False, None
        frontier = [by_id[k] for k in keys]
    return True, None


def evidenced(conn, target):
    """True once any lifecycle event of ``target`` carries causal evidence."""
    from hermes_cli.kanban_resume_exec import _LIFECYCLE_KINDS
    return any(metadata(r) for r in events(conn, target, _LIFECYCLE_KINDS))


def park_identity(conn, target):
    row = latest(conn, target, ('blocked',))
    if not synced(conn):
        return row['id'] if row else 0
    return identity(row)


def _observed_parks(conn, creator):
    from hermes_cli.kanban_resume_exec import MAX_CREATOR_HOPS, _creator
    result = {}
    for _ in range(MAX_CREATOR_HOPS):
        if not creator:
            break
        park = park_identity(conn, creator)
        if park:
            result[creator] = park
        creator = _creator(conn, creator)
    return result


def stamp(conn, task_id, kind, payload, run_id):
    from hermes_cli.kanban_resume_exec import _LIFECYCLE_KINDS
    receipts = ('continuation_exhausted', 'goal_unachievable')
    if kind not in (*_LIFECYCLE_KINDS, *receipts, 'created') or not synced(conn):
        return payload
    data: dict = {'identity': str(uuid.uuid4())}
    if kind in _LIFECYCLE_KINDS:
        data['predecessors'] = [identity(r) for r in heads(events(conn, task_id, _LIFECYCLE_KINDS))]
    if kind == 'created':
        data['parks'] = _observed_parks(conn, (payload or {}).get('creator_task_id'))
    if kind == 'blocked' and run_id is not None:
        data['receipts'] = [identity(r) for r in events(conn, task_id, receipts) if r['run_id'] == run_id]
    return {**(payload or {}), META: data}


def fresh(conn, requester, target):
    rows = events(conn, requester, ('created',))
    park = park_identity(conn, target)
    return bool(park and len(rows) == 1 and metadata(rows[0]).get('parks', {}).get(target) == park)


def receipt_precedes(conn, receipt, park):
    if not synced(conn):
        return receipt['id'] < park['id']
    return identity(receipt) in metadata(park).get('receipts', [])
