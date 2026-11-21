"""Read-only priority donation across a partial executor board and its replica."""
import logging
import sqlite3
from collections import defaultdict

from hermes_cli.kanban_priority import PRIORITY_CAP

_LOG = logging.getLogger(__name__)


def order_with_remote_waiters(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Keep remote descendants in dispatch ranking without importing or editing them.

    Local rows override replica rows, and a child's local edge set overrides its
    replicated edges (including an unlinked empty set). This preserves changes
    not yet pushed to the Board. The Board still authorizes every actual claim.
    """
    try:
        from hermes_cli.kanban_admission_score import rank

        ranked = rank(conn, rows)
        from hermes_cli.kanban_candidate_receipts import scores
        scores(ranked)
        return [row for row, _parts in ranked]
    except (sqlite3.Error, OSError, ValueError):
        _LOG.warning("could not score dispatch candidates; using priority ordering", exc_info=True)
    try:
        return _order_with_remote_waiters(conn, rows)
    except Exception:  # health: allow BLE001 -- advisory replica reads must never stop local dispatch
        _LOG.warning("could not read replica priority graph; using local ordering", exc_info=True)
        return rows


def _field(row, key: str):
    """``row[key] or 0``, and 0 when the row has no such column."""
    try:
        return row[key] or 0
    except (IndexError, KeyError):
        return 0


def _order_with_remote_waiters(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn

    if not rows or sync.board_mode(conn) != "primary":
        return rows
    replica = wn.open_replica(conn)
    if replica is None:
        return rows
    try:
        remote = replica.execute(
            "SELECT id, priority, status, created_at FROM tasks WHERE status NOT IN ('done', 'archived')"
        ).fetchall()
        edges = replica.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    finally:
        replica.close()
    local = conn.execute("SELECT id, priority, status, created_at FROM tasks").fetchall()
    local_ids = {r["id"] for r in local}
    tasks = {r["id"]: r for r in remote}
    tasks.update((r["id"], r) for r in local)
    edges = [(p, c) for p, c in edges if c not in local_ids]
    edges.extend(conn.execute("SELECT parent_id, child_id FROM task_links").fetchall())
    children = defaultdict(set)
    for parent, child in edges:
        if child in tasks and tasks[child]["status"] not in ("done", "archived"):
            children[parent].add(child)

    def rank(row):
        tid = row["id"]
        # A candidate listed from an earlier replica snapshot may be done in
        # this one (filtered out of ``tasks``): rank it by its own row. Not
        # seeded into ``tasks``, whose rows must carry ``status`` for the
        # child-graph filter above. [daveey]
        task = tasks.get(tid, row)
        priority = _field(task, "priority")
        pending = list(children[tid])
        seen = {tid}
        while pending:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            priority = max(priority, tasks[child]["priority"] or 0)
            pending.extend(children[child] - seen)
        return -min(PRIORITY_CAP, priority), -bool(children[tid]), _field(task, "created_at")

    return sorted(rows, key=rank)
