"""P2-retained run snapshots with an exact per-read bookkeeping overlay."""
import itertools
import marshal
import operator
from types import MappingProxyType

# P2 excludes these run columns. Even historical terminal runs need the overlay:
# unused-grant classification reads NULL-ness of four of them at projection time.
_BOOKKEEPING = ("claim_lock", "claim_expires", "last_heartbeat_at", "worker_pid",
                "worker_started_at", "sync_version")


def retained_runs(conn, ids, prev, changed, load):
    """Return all run fields without decoding unchanged historical metadata again.

    ``changed=None`` is the legacy/untrusted path. The caller owns the same-snapshot
    cursor contract and replaces the memo on schema or connection generation change.
    """
    if changed is None:
        return load(conn, "task_runs", ids)
    dirty = [tid for tid in ids if tid in changed or tid not in prev or len(prev[tid]) < 5]
    result = load(conn, "task_runs", dirty) if dirty else {}
    clean = [tid for tid in ids if tid not in result]
    if not clean:
        return result
    old = {tid: prev[tid][4] for tid in clean}
    # Old schemas can lack a newer bookkeeping column. Schema changes force FULL,
    # so retained rows describe the selected columns for this snapshot generation.
    keys = next((row.keys() for rows in old.values() for row in rows), ())
    columns = [key for key in _BOOKKEEPING if key in keys]
    signatures = _signatures(conn, clean, columns)
    stale = [tid for tid in clean if getattr(old[tid], "signature", None) != signatures.get(tid, "")]
    stale_set = set(stale)
    for tid in clean:
        if tid not in stale_set:
            result[tid] = old[tid]  # every run id and bookkeeping value (type included) unchanged
    if not stale:
        return result
    selected = ", ".join(("id", "task_id", *columns))
    rows_by_task = {tid: [] for tid in stale}
    for row in conn.execute(
            f"SELECT {selected} FROM task_runs WHERE task_id IN ({','.join('?' for _ in stale)}) ORDER BY id",
            tuple(stale)):
        rows_by_task[row[1]].append(tuple(row))
    reload = []
    for tid in stale:
        before, overlay = old[tid], rows_by_task[tid]
        if len(before) != len(overlay) or any(a["id"] != b[0] for a, b in zip(before, overlay)):
            reload.append(tid)  # fail exact if a caller supplied an incomplete invalidation set
            continue
        rows = []
        for original, row in zip(before, overlay):
            # Same typed encoding as the signature, including signed floating zero
            # on legacy untyped columns. Never stamp bytes for values not retained.
            updates = {key: value for key, value in zip(columns, row[2:])
                       if marshal.dumps(original[key], 2) != marshal.dumps(value, 2)}
            rows.append(MappingProxyType({**original, **updates}) if updates else original)
        # Read before these values, so a later write can only make the next compare miss.
        result[tid] = RunHistory(rows, signatures.get(tid, ""))
    if reload:
        result.update(load(conn, "task_runs", reload))
    return result


class RunHistory(tuple):
    """Retained runs plus the packed signature of their ids and bookkeeping values."""

    signature: bytes | str

    def __new__(cls, rows, signature):
        self = super().__new__(cls, rows)
        self.signature = signature
        return self


def _signatures(conn, task_ids, columns) -> dict:
    """Exact typed run values of each task, one row per task.

    SQLite builds each task's text in C (``board_event_history.row_text``: storage class plus
    hex bytes, so NULL/int/float/text/blob and embedded NULs stay distinct) over the covering
    ``idx_runs_task_bookkeeping`` index. Every GET after a bookkeeping commit signs every run of
    every displayed card here; stepping each run row into Python (1.3k-3k rows on the hub board,
    each one a GIL hand-off in the busy backend) was the cost. Signatures are only compared
    in-process. Without ordered aggregates (SQLite < 3.44) runs are packed with ``marshal``
    (version 2: equal values encode equally regardless of object sharing).
    """
    if not task_ids:
        return {}
    from plugins.kanban.dashboard.board_event_history import ORDERED_AGGREGATES, row_text
    if ORDERED_AGGREGATES:
        cur = conn.cursor()
        cur.row_factory = None
        return dict(cur.execute(
            f"SELECT task_id, group_concat({row_text(('id', *columns))}, ',' ORDER BY id) FROM task_runs "
            f"WHERE task_id IN ({','.join('?' for _ in task_ids)}) GROUP BY task_id", tuple(task_ids)).fetchall())
    selected = ", ".join(("task_id", "id", *columns))
    # Plain tuples, grouped in C: every GET after a bookkeeping commit reads every run of
    # every displayed card here, and per-row Row objects were most of its cost.
    cur = conn.cursor()
    cur.row_factory = None
    rows = cur.execute(
        f"SELECT {selected} FROM task_runs "
        f"WHERE task_id IN ({','.join('?' for _ in task_ids)}) ORDER BY task_id, id",
        tuple(task_ids)).fetchall()
    return {tid: marshal.dumps([row[1:] for row in group], 2)
            for tid, group in itertools.groupby(rows, key=operator.itemgetter(0))}
