"""Discover a forwarding creator identity; the origin gateway authenticates it.

Replica ownership is a routing hint, not permission. Only the origin-local
binding plus the server-owned lease can authorize the resulting request.
"""
from __future__ import annotations

from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_execution_observations import board_owner_profile
from hermes_cli.kanban_origin import _replica_task_owners
from hermes_cli.kanban_placement_forward import opened_board


def forwarding_authority(conn, task_id, run_id, *, self_gw):
    """Require evidenced owner namespace and portable run identity, never guess."""
    if conn is None or not task_id or not self_gw:
        raise ValueError("origin_forward_creator_unknown")
    board = opened_board(conn)
    cols = sync.table_columns(conn, "tasks")
    if "created_on" not in cols:
        raise ValueError("origin_forward_creator_unknown")
    row = conn.execute("SELECT created_on FROM tasks WHERE id=?", (task_id,)).fetchone()
    gateway = row[0] if row else None
    if not gateway:
        raise ValueError("origin_forward_creator_unknown")
    if gateway == self_gw:
        path = sync._db_key(conn)
        profile = board_owner_profile(path) if path else None
    else:
        profiles = {profile for gw, profile in _replica_task_owners(task_id, board) if gw == gateway}
        profile = next(iter(profiles)) if len(profiles) == 1 else None
    run = conn.execute("SELECT task_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
    uid = sync.run_uid(conn, run_id) if run and run[0] == task_id else None
    if not profile or not uid:
        raise ValueError("origin_forward_creator_unknown")
    return {"creator": {"gateway": gateway, "profile": profile, "board": board, "task_id": task_id},
            "run_uid": uid}
