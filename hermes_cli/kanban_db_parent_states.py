"""Single dependency status view, including remote mirrors and terminal holds."""
from __future__ import annotations

import sqlite3


def parent_blocks(conn: sqlite3.Connection, pid: str, status: str | None) -> bool:
    """Mirror hermes-hub worker/src/transitions.ts parentsSatisfied: terminal is not always success."""
    if status not in ("done", "archived"):
        return True
    if status == "archived":
        row = conn.execute("SELECT containment_episode FROM tasks WHERE id=?", (pid,)).fetchone()
        if row is not None and row[0] is not None:
            return True
    from hermes_cli.kanban_spend_decision import execution_restricted
    return execution_restricted(conn, pid)


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
    from hermes_cli.kanban_containment_terminal import parent_status_sql
    if remote is None:
        remote = sync.has_remote_parents(conn)
    parent_status = parent_status_sql(conn, "p")
    if remote:
        sql = (f"SELECT l.parent_id AS id, COALESCE({parent_status}, r.status) AS status FROM task_links l "
               "LEFT JOIN tasks p ON p.id = l.parent_id "
               f"LEFT JOIN {sync.REMOTE_PARENTS_TABLE} r ON r.parent_id = l.parent_id "
               "WHERE l.child_id = ? ORDER BY l.parent_id")
    else:
        sql = (f"SELECT l.parent_id AS id, {parent_status} AS status FROM task_links l "
               "LEFT JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ? ORDER BY l.parent_id")
    return [(row["id"], row["status"]) for row in conn.execute(sql, (task_id,)).fetchall()]
