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
    selected = ", ".join(("id", "task_id", *columns))
    fresh = {tid: [] for tid in clean}
    for row in conn.execute(
            f"SELECT {selected} FROM task_runs WHERE task_id IN ({','.join('?' for _ in clean)}) ORDER BY id",
            tuple(clean)):
        fresh[row["task_id"]].append(row)
    reload = []
    for tid in clean:
        before, overlay = old[tid], fresh[tid]
        if len(before) != len(overlay) or any(a["id"] != b["id"] for a, b in zip(before, overlay)):
            reload.append(tid)  # fail exact if a caller supplied an incomplete invalidation set
            continue
        rows = []
        for original, row in zip(before, overlay):
            updates = {key: row[key] for key in columns if row[key] != original[key]}
            rows.append(MappingProxyType({**original, **updates}) if updates else original)
        result[tid] = tuple(rows)
    if reload:
        result.update(load(conn, "task_runs", reload))
    return result
