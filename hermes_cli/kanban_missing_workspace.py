"""Fail closed on a lost host-local workspace binding before legacy resolution.

A created event is synced history, not permission to run its path on this host.
Use it as a diagnostic only; never silently choose another worktree or source.
"""
from __future__ import annotations

import json
from pathlib import Path


def check(conn, task) -> None:
    """A missing dir path, or a worktree whose creation bound a path, is sticky.

    Intentionally unbound worktrees retain the existing board-default resolver.
    Source-protocol grants never enter this legacy-only check.
    """
    if task.workspace_path or task.workspace_kind not in ("dir", "worktree"):
        return
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1",
                       (task.id,)).fetchone()
    bound = None
    if row is not None:
        try:
            payload = json.loads(row[0])
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            value = payload.get("workspace_path")
            if isinstance(value, str) and value:
                bound = value
    if task.workspace_kind == "worktree" and bound is None:
        return
    detail = ""
    if bound is not None:
        # Do not expand a foreign host's tilde or interpret relative paths here.
        path = Path(bound)
        try:
            exists = path.is_absolute() and path.is_dir()
        except OSError:
            exists = False
        detail = f" Created event bound {bound!r} (directory {'exists' if exists else 'not verified'} on this host)."
    from hermes_cli.kanban_db_workspace import WorktreeWorkspaceConfigError
    raise WorktreeWorkspaceConfigError(
        f"task {task.id} has missing local workspace binding (workspace_kind={task.workspace_kind})."
        f"{detail} Verify source custody on the executing host, restore its absolute workspace_path "
        "with set_workspace_path, then explicitly release the card. A synced created path is not host authorization."
    )
