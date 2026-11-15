"""P2-retained run snapshots with an exact per-read bookkeeping overlay."""
import marshal
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
    """Exact typed run values, packed in C instead of SQL quote/concatenation.

    These bytes are only compared in-process, never persisted or unmarshalled.
    Version 2 avoids reference-identity encoding: equal values must encode equally
    regardless of Python object sharing. It preserves SQLite NULL/int/float/text/blob,
    including embedded NULs that SQLite quote(text) truncates.
    """
    if not task_ids:
        return {}
    selected = ", ".join(("task_id", "id", *columns))
    grouped = {}
    for row in conn.execute(
            f"SELECT {selected} FROM task_runs "
            f"WHERE task_id IN ({','.join('?' for _ in task_ids)}) ORDER BY task_id, id",
            tuple(task_ids)):
        grouped.setdefault(row[0], []).append(tuple(row)[1:])
    return {tid: marshal.dumps(rows, 2) for tid, rows in grouped.items()}
