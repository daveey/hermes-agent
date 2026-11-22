"""Exact, storage-type-preserving history comparison in the caller's snapshot."""
from __future__ import annotations

import marshal
from itertools import groupby
from operator import itemgetter
from types import MappingProxyType


def _raw(history):
    return tuple(tuple(row.values()) if isinstance(row, MappingProxyType) else tuple(row) for row in history)


def same_histories(conn, histories: dict) -> dict[str, bool]:
    """One SQL read for all ambiguous P2 invalidations; no history reductions.

    Keep the caller's snapshot: opening another connection here could compare
    against a newer commit than its task rows and event marks. Close the cursor
    before Python comparisons; the caller ends its snapshot before projection.
    """
    if not histories:
        return {}
    cur = conn.cursor()
    cur.row_factory = None
    try:
        rows = cur.execute(
            f"SELECT * FROM task_events WHERE task_id IN ({','.join('?' for _ in histories)})",
            tuple(histories)).fetchall()
        keys = tuple(d[0] for d in cur.description)
    finally:
        cur.close()
    matches = {tid: not history for tid, history in histories.items()}
    # idx_events_task is (task_id, created_at), not (task_id, id). Avoid
    # SQLite's wide-row temporary B-tree; order detached tuples in Python.
    rows.sort(key=itemgetter(keys.index('task_id'), keys.index('id')))
    for tid, group in groupby(rows, itemgetter(keys.index('task_id'))):
        history = histories[tid]
        matches[tid] = (not history or tuple(history[0].keys()) == keys) and (
            marshal.dumps(tuple(group), 2) == marshal.dumps(_raw(history), 2))
    return matches


def same_events(conn, tid, history) -> bool:
    """Scalar compatibility helper, also the pre-batch parity oracle."""
    cur = conn.cursor()
    cur.row_factory = None
    try:
        rows = cur.execute('SELECT * FROM task_events WHERE task_id = ? ORDER BY id', (tid,)).fetchall()
        keys = tuple(d[0] for d in cur.description)
        if history and tuple(history[0].keys()) != keys:
            return False
        return marshal.dumps(tuple(rows), 2) == marshal.dumps(_raw(history), 2)
    finally:
        cur.close()
