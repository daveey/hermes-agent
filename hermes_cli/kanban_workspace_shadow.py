"""Name the workspace file that shadowed a Python stdlib module in a dead worker. [daveey]

A Kanban worker starts inside its task workspace, where agents write scratch scripts. A
helper named like a stdlib module (``inspect.py``) used to win over the real module when
the worker started (``python -m`` puts the cwd first on ``sys.path``), so every respawn
ran the helper and exited before the agent did anything. The crash was booked as a plain
``exited with code 1`` and retried until the breaker gave up; supervisors chased unrelated
log lines. The module-form worker now runs with ``-P``; this is the detection layer for any
launch path that still trusts the cwd:

* ``startup_shadows`` lists top-level ``<stdlib-name>.py`` files in a workspace when the run
  died before its first heartbeat, so the crash error says which file to rename;
* ``traceback_hits`` reports the ones the worker's log tail shows a traceback frame from,
  which proves the shadow caused the death: the dispatcher blocks that card at once instead
  of retrying a deterministic failure.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Optional

# Names a top-level script commonly carries without being imported by anything at start-up.
_IGNORED = frozenset({"__main__", "this", "antigravity"})


def stdlib_shadows(workspace: Optional[Path]) -> list[str]:
    """Top-level ``*.py`` files in ``workspace`` whose stem is a stdlib module name, sorted."""
    if workspace is None:
        return []
    try:
        entries = list(workspace.iterdir())
    except OSError:
        return []
    names = sys.stdlib_module_names
    return sorted(
        p.name for p in entries
        if p.suffix == ".py" and p.stem in names and p.stem not in _IGNORED and p.is_file()
    )


def _workspace_for(conn: sqlite3.Connection, task_id: str, board: Optional[str]) -> Optional[Path]:
    from hermes_cli import kanban_db as kb

    row = conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    raw = row[0] if row is not None else None
    path = Path(raw).expanduser() if raw else kb.workspaces_root(board=board) / task_id
    return path if path.is_absolute() and path.is_dir() else None


def _run_had_heartbeat(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> bool:
    if run_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'heartbeat' LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    return row is not None


def startup_shadows(
    conn: Optional[sqlite3.Connection], task_id: str, run_id: Optional[int], board: Optional[str],
) -> tuple[Optional[Path], list[str]]:
    """``(workspace, shadow file names)`` for a run that died before its first heartbeat.

    Returns ``(None, [])`` when the run heartbeat (it got past start-up) or nothing shadows.
    Never raises: this only adds a hint to a crash that is already being booked.
    """
    if conn is None:
        return None, []
    try:
        if _run_had_heartbeat(conn, task_id, run_id):
            return None, []
        workspace = _workspace_for(conn, task_id, board)
        return workspace, stdlib_shadows(workspace)
    except (sqlite3.Error, OSError, ValueError):
        return None, []


def traceback_hits(workspace: Path, shadows: list[str], log_tail: str) -> list[str]:
    """Shadow files that appear as a traceback frame (``File "<workspace>/<name>"``) in the log."""
    hits = []
    for name in shadows:
        frame = f'File "{workspace / name}"'
        if frame in log_tail:
            hits.append(name)
    return hits


def hint(shadows: list[str], proven: list[str]) -> str:
    """One sentence for the crash error naming each shadow file and the fix."""
    parts = [f"{name} shadows stdlib module {name[:-3]}" for name in shadows]
    lead = "Worker died at start-up because workspace file " if proven else (
        "Worker died before its first heartbeat; workspace file ")
    return (f" {lead}{'; '.join(parts)}. Rename it (e.g. {shadows[0][:-3]}_helper.py) "
            "and send the card back to ready.")


def note_dead_worker(dead, conn, task_id: str, run_id: Optional[int], board: Optional[str]) -> None:
    """Append the shadow hint to a dead worker's crash error; flag it to block at once when proven."""
    from hermes_cli import kanban_db as kb

    workspace, shadows = startup_shadows(conn, task_id, run_id, board)
    if not shadows or workspace is None:
        return
    log_tail = kb.read_worker_log(task_id, tail_bytes=16000, board=board) or ""
    proven = traceback_hits(workspace, shadows, log_tail)
    dead.error_text += hint(shadows, proven)
    dead.event_payload["workspace_stdlib_shadows"] = shadows
    dead.stdlib_shadow = bool(proven)
