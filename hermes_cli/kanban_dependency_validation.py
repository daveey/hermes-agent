"""Refuse unresolved dependency edges before ending a worker's run."""
import sqlite3


def insert_link(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    """Validate internal graph writes too, inside the caller's transaction.

    Cross-board dependencies use explicit waits; sync imports retain their own
    replica-aware ingestion path rather than manufacturing local task rows.
    """
    from hermes_cli.kanban_db import _missing_task_ids

    missing = _missing_task_ids(conn, [parent_id, child_id])
    if missing:
        raise missing_dependency_error(missing)
    from hermes_cli.kanban_helper_parent import check_helper_parent

    check_helper_parent(conn, parent_id, child_id)
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )
    from hermes_cli.kanban_lanes import release_blocker

    release_blocker(conn, parent_id, child_id)


def would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True iff ``parent_id`` is already a descendant of ``child_id``."""
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def missing_dependency_error(ids):
    return ValueError(
        f"unknown task(s): {', '.join(ids)}; not on this board. "
        "For a parent on another gateway, unlink any dangling edge, then use "
        "kind='time', awaiting={kind:'card', ref:'<parent-id>', gateway:'<owner-gateway>'}. "
        "The card wait supplies an owner-aware wake probe automatically "
        "(kanban_wait.py task <id> over ssh); do not create a local dependency edge."
    )


def validate_dependency_block(conn, task_id, kind):
    if kind != "dependency":
        return
    from hermes_cli.kanban_db import parent_states

    # Synced-board foreign parents are valid when the replica resolves their state.
    missing = [parent for parent, state in parent_states(conn, task_id) if state is None]
    if missing:
        raise missing_dependency_error(missing)


def check_link_parent(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    """Refuse an edge that would cycle or hang under a never-finishing standing card. [daveey]"""
    if would_cycle(conn, parent_id, child_id):
        raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
    from hermes_cli.kanban_helper_parent import check_helper_parent

    check_helper_parent(conn, parent_id, child_id)
    from hermes_cli.kanban_standing_parent import standing_parent_error, standing_reason
    standing = standing_reason(conn, parent_id)
    if standing:
        raise standing_parent_error(parent_id, child_id, standing)
