"""Single dependency status view, including remote mirrors and terminal holds."""
from __future__ import annotations

import sqlite3


def parent_blocks(conn: sqlite3.Connection, pid: str, status: str | None) -> bool:
    """Mirror hermes-hub worker/src/transitions.ts parentsSatisfied: terminal is not always success."""
    if status not in ("done", "archived"):
        return True
    if status == "archived":
        from hermes_cli.kanban_containment_terminal import parent_status_sql
        projected = parent_status_sql(conn)
        row = conn.execute(f"SELECT {projected} FROM tasks WHERE id=?", (pid,)).fetchone()
        if row is not None and row[0] != "archived":
            return True
    from hermes_cli.kanban_spend_decision import execution_restricted
    return execution_restricted(conn, pid)


# Every event kind ``execution_restricted`` reads on the parent itself: a terminal parent with none of
# them cannot be restricted, so the exact (per-parent, multi-statement) check runs only on candidates.
RESTRICTION_EVIDENCE_KINDS = ("administrative_stop_restricted", "spend_budget_triaged",
                              "decision_hold_restricted", "decision_retained")


def parent_blocks_many(conn: sqlite3.Connection, pairs) -> dict[str, bool]:
    """``parent_blocks`` for many ``(parent, status)`` pairs in a bounded number of statements.

    Read-only presentation readers only. Statement count does not grow with ordinary terminal parents:
    one archived-projection read, one restriction-evidence prefilter, then the exact ``parent_blocks``
    check for each parent that actually carries restriction evidence (genuinely held parents).
    """
    import json

    from hermes_cli.kanban_containment_terminal import parent_status_sql
    from hermes_cli.kanban_stop_ended import KIND
    kinds = tuple(dict.fromkeys((KIND, *RESTRICTION_EVIDENCE_KINDS)))  # the stop kind is read from its owner
    status_of = dict(pairs)
    out = {pid: True for pid, status in status_of.items() if status not in ("done", "archived")}
    terminal = [pid for pid in status_of if pid not in out]
    if not terminal:
        return out
    archived = [pid for pid in terminal if status_of[pid] == "archived"]
    if archived:
        projected = parent_status_sql(conn)
        for pid, status in conn.execute(f"SELECT id, {projected} FROM tasks WHERE id IN (SELECT value FROM json_each(?))",
                                        (json.dumps(archived),)).fetchall():
            if status != "archived":
                out[pid] = True
    rest = [pid for pid in terminal if pid not in out]
    if not rest:
        return out
    marks = ",".join("?" for _ in kinds)
    try:
        candidates = {row[0] for row in conn.execute(
            f"SELECT DISTINCT task_id FROM task_events WHERE task_id IN (SELECT value FROM json_each(?)) "
            f"AND kind IN ({marks})", (json.dumps(rest), *kinds)).fetchall()}
    except sqlite3.OperationalError:
        candidates = set(rest)  # no prefilter evidence: fall back to the exact check for every parent
    for pid in rest:
        out[pid] = parent_blocks(conn, pid, status_of[pid]) if pid in candidates else False
    return out


def mirrored_status(conn: sqlite3.Connection, pid: str, status: str) -> str:
    """Freeze restriction evidence in the status-only foreign-parent mirror, in the replica snapshot."""
    return f"{status}:restricted" if status in ("done", "archived") and parent_blocks(conn, pid, status) else status


def parent_states(conn: sqlite3.Connection, task_id: str, *, remote: bool | None = None) -> list[tuple[str, str | None]]:
    """Direct prerequisite statuses; missing and containment-archived parents gate.

    Primary files retain only local cards; foreign statuses come from the mirror
    refreshed from one canonical replica snapshot. Ordinary archived parents are
    still satisfied. A terminal containment archive is non-success, not release.
    """
    from hermes_cli import kanban_db_sync as sync
    if remote is None:
        remote = sync.has_remote_parents(conn)
    sql = _states_sql(conn, remote, "l.child_id = ?")
    return [(row[1], row[2]) for row in conn.execute(sql, (task_id,)).fetchall()]


def parent_states_many(conn: sqlite3.Connection, task_ids) -> dict[str, list[tuple[str, str | None]]]:
    """``parent_states`` for many children in one statement (read-only presentation readers)."""
    import json

    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_table_columns import tables_columns
    ids = [str(t) for t in dict.fromkeys(task_ids)]
    out: dict[str, list[tuple[str, str | None]]] = {tid: [] for tid in ids}
    if not ids:
        return out
    links, remote = tables_columns(conn, "task_links", sync.REMOTE_PARENTS_TABLE)  # one row for both probes
    if not links:
        return out  # a board without the link table has no prerequisites
    sql = _states_sql(conn, bool(remote), "l.child_id IN (SELECT value FROM json_each(?))")
    for child, pid, status in conn.execute(sql, (json.dumps(ids),)).fetchall():
        out[str(child)].append((pid, status))
    return out


def _states_sql(conn: sqlite3.Connection, remote: bool, child_filter: str) -> str:
    """One status projection for single and batched readers, so they cannot drift."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_containment_terminal import parent_status_sql
    parent_status = parent_status_sql(conn, "p")
    if remote:
        return (f"SELECT l.child_id AS child, l.parent_id AS id, COALESCE({parent_status}, r.status) AS status "
                "FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id "
                f"LEFT JOIN {sync.REMOTE_PARENTS_TABLE} r ON r.parent_id = l.parent_id "
                f"WHERE {child_filter} ORDER BY l.child_id, l.parent_id")
    return (f"SELECT l.child_id AS child, l.parent_id AS id, {parent_status} AS status FROM task_links l "
            f"LEFT JOIN tasks p ON p.id = l.parent_id WHERE {child_filter} ORDER BY l.child_id, l.parent_id")
