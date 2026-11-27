"""[daveey] P2-retained runs with response-exact, not row-exact bookkeeping.

The only response reader of these rows is ``board_read.diagnostics_from_inputs``
(including shadow and owner/fact capture), via ``kanban_diagnostics``. Its only
bookkeeping reader is ``kanban_unused_grant.unused_grant_run``: four NULL tests,
behind P2-marked preconditions. Share that reader's candidate predicate and read
set. Any future diagnostic bookkeeping reader must extend this overlay contract.

Clean retained rows may carry stale bookkeeping values the projection never
reads (including claim_expires/sync_version, noncandidate rows, and non-NULL
value changes). They are NOT general-purpose fresh run snapshots. P2 covers run
inserts/deletes and all candidate-precondition changes; the caller must supply a
trusted change set in the same SQLite snapshot. Legacy and dirty reads stay full.
"""
import json
from types import MappingProxyType

from hermes_cli.kanban_unused_grant import UNUSED_GRANT_BOOKKEEPING, unused_grant_candidate


def retained_runs(conn, ids, prev, changed, load):
    """[daveey] Retain diagnostic inputs, refreshing only response-visible NULL tests.

    ``changed=None`` is the legacy/untrusted path. The caller owns the same-snapshot
    cursor contract and replaces the memo on schema or connection generation change.
    """
    if changed is None:
        return load(conn, "task_runs", ids)
    dirty = [tid for tid in ids if tid in changed or tid not in prev or len(prev[tid]) < 5]
    result = load(conn, "task_runs", dirty) if dirty else {}
    clean = [tid for tid in ids if tid not in result]
    old = {tid: (prev[tid][4] if isinstance(prev[tid][4], RunHistory) else RunHistory(prev[tid][4]))
           for tid in clean}
    candidates = {row["id"]: row for history in old.values() for row in history.candidates}
    if not candidates:
        return {**result, **old}
    # [daveey] Schema changes force FULL; older schemas may omit bookkeeping columns.
    columns = [key for key in UNUSED_GRANT_BOOKKEEPING if key in next(iter(candidates.values()))]
    signatures = _signatures(conn, list(candidates), columns)
    stale = [rid for rid, row in candidates.items()
             if tuple(row[key] is not None for key in columns) != signatures.get(rid)]
    overlays = _overlays(conn, stale, columns)
    for tid, history in old.items():
        if any(row["id"] not in signatures for row in history.candidates):
            # [daveey] Fail exact on a missing candidate even if a caller violated P2.
            result.update(load(conn, "task_runs", [tid]))
        elif any(row["id"] in overlays for row in history.candidates):
            result[tid] = RunHistory(MappingProxyType({**row, **overlays[row["id"]]})
                                     if row["id"] in overlays else row for row in history)
        else:
            result[tid] = history
    return result


class RunHistory(tuple):
    """[daveey] Immutable rows and their P2-stable unused-grant candidates."""

    candidates: tuple

    def __new__(cls, rows):
        self = super().__new__(cls, rows)
        self.candidates = tuple(row for row in self if unused_grant_candidate(row))
        return self


def _signatures(conn, run_ids, columns) -> dict:
    """[daveey] Indexed candidate-ID seeks; only NULL-ness crosses into Python."""
    selected = ", ".join(("id", *(f"{key} IS NOT NULL" for key in columns)))
    cur = conn.cursor()
    cur.row_factory = None
    return {row[0]: row[1:] for row in cur.execute(
        f"SELECT {selected} FROM task_runs WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(run_ids),))}


def _overlays(conn, run_ids, columns) -> dict:
    """[daveey] Fetch actual values only when their response-visible NULL-ness moved."""
    if not run_ids:
        return {}
    selected = ", ".join(("id", *columns))
    return {row[0]: dict(zip(columns, tuple(row)[1:])) for row in conn.execute(
        f"SELECT {selected} FROM task_runs WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(run_ids),))}
