"""Snapshot-local graph inputs for the parked/held annotation walk."""
import json
import sqlite3


def capture(conn, pairs, cols):
    """Exhaust raw graph inputs in the caller's snapshot, without Python UDFs.

    Include unowned unfinished ancestors: they can hold an owned descendant.
    The caller's active pairs already apply subscription/watch/provenance rules.
    """
    pairs = set(pairs)
    if not pairs or 'session_id' not in cols:
        return set(), [], {}
    kind = 't.block_kind' if 'block_kind' in cols else 'NULL'
    request = 't.human_request' if 'human_request' in cols else 'NULL'
    rows = list(conn.execute(
        f'SELECT t.id, t.status, {kind}, {request} FROM tasks t '
        "WHERE t.status NOT IN ('done','archived')"))
    try:
        parents = open_parent_edges(conn, [row[0] for row in rows])
    except sqlite3.Error:  # board predates dependencies
        parents = {}
    return pairs, rows, parents


def derive(inputs, predicate):
    """Classify and walk detached rows after the source reader has closed."""
    pairs, rows, parents = inputs
    info = {str(tid): (status, bool(predicate(status, kind, request)))
            for tid, status, kind, request in rows}

    def open_parents(tid):
        return [p for p in parents.get(tid, ()) if p in info]

    def waits_on_human(tid):
        seen = set()
        stack = open_parents(tid)
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            status, human = info[parent]
            if status in ('blocked', 'triage') and human:
                return True
            stack.extend(open_parents(parent))
        return False

    parked, held = {}, {}
    parked_pairs, held_pairs = set(), set()
    for sid, tid in pairs:
        status = info.get(tid, ('', False))[0]
        if status == 'todo':
            if not open_parents(tid):
                continue
            human = waits_on_human(tid)
        elif status in ('scheduled', 'blocked', 'triage'):
            human = False
        else:
            continue
        counts, captured = (held, held_pairs) if human else (parked, parked_pairs)
        counts[sid] = counts.get(sid, 0) + 1
        captured.add((sid, tid))
    return parked, held, parked_pairs, held_pairs


def open_parent_edges(conn, unfinished):
    """Read only edges the walk can follow; never fetch the historical graph.

    ``unfinished`` is the caller's already-read task-status map. Passing its IDs
    avoids scanning tasks again and keeps parent selection on that same view.
    No memo or connection survives this pass. Missing legacy tables still raise
    to the caller's existing compatibility handler.
    """
    parents = {}
    for parent, child in conn.execute(
            'SELECT parent_id, child_id FROM task_links '
            'WHERE parent_id IN (SELECT value FROM json_each(?))', (json.dumps(list(unfinished)),)):
        parents.setdefault(str(child), []).append(str(parent))
    return parents
