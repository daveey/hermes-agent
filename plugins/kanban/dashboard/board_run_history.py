"""P2-retained run snapshots with an exact per-read bookkeeping overlay."""
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
            # Type-exact: 1 -> 1.0 or 0 -> False is a change the retained row must show.
            updates = {key: value for key, value in zip(columns, row[2:])
                       if (prior := original[key]) != value or type(prior) is not type(value)}
            rows.append(MappingProxyType({**original, **updates}) if updates else original)
        # Read before these values, so a later write can only make the next compare miss.
        result[tid] = RunHistory(rows, signatures.get(tid, ""))
    if reload:
        result.update(load(conn, "task_runs", reload))
    return result


class RunHistory(tuple):
    """Retained runs of one task plus the SQL signature of their ids and bookkeeping values."""

    signature: str

    def __new__(cls, rows, signature):
        self = super().__new__(cls, rows)
        self.signature = signature
        return self


def _signatures(conn, task_ids, columns) -> dict:
    """``{task_id: text}`` naming every run id and bookkeeping value, ``quote``d so type and
    NULL-ness are exact, built in SQLite rather than by comparing each run in Python."""
    if not task_ids:
        return {}
    fields = "||','||".join(f"quote({c})" for c in ("id", *columns))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT task_id, group_concat(sig, ';') FROM (SELECT task_id, {fields} AS sig FROM task_runs "
        f"WHERE task_id IN ({','.join('?' for _ in task_ids)}) ORDER BY task_id, id) GROUP BY task_id",
        tuple(task_ids))}
