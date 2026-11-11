"""Call a dispatcher ``spawn_fn`` whatever its signature (moved out of ``kanban_db_dispatch``). [daveey]"""
from __future__ import annotations

from typing import Any, Optional


def call_spawn_fn(spawn_fn, task: Any, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)
