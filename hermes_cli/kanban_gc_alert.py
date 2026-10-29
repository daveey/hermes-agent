"""One Kanban card when workspace GC can't read a board. [daveey]

An aborted startup worktree sweep, or a cleanup deferred because a board db
can't be read ("cannot tell whether another task still uses"), used to only log
a warning; finished-task workspaces then piled up unnoticed (kanban-e808885f9e).
Each such failure now opens ONE card (``idempotency_key='kanban-worktree-gc-abort'``)
naming the db path; later occurrences comment on it, at most once an hour per path.
A finished card does not swallow a recurrence: a fresh card is opened instead.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

log = logging.getLogger(__name__)

KEY = "kanban-worktree-gc-abort"
AUTHOR = "kanban-worktree-gc"
_COMMENT_EVERY = 3600


def report(conn: Optional[sqlite3.Connection], db_path: str, detail: str) -> Optional[str]:
    """Open or comment the GC-abort card on *conn*'s board (the default board when None).

    Best effort: never raises, since it runs on cleanup/startup paths.
    """
    try:
        if conn is None:
            from hermes_cli import kanban_db_connect as kbc
            from hermes_cli import kanban_db as kb
            with kbc.connect_closing(kb.kanban_home() / "kanban.db") as own:
                return _report(own, db_path, detail)
        return _report(conn, db_path, detail)
    except Exception:
        log.warning("kanban: could not file the worktree-GC abort card for %s", db_path, exc_info=True)
        return None


def _report(conn: sqlite3.Connection, db_path: str, detail: str) -> str:
    from hermes_cli import kanban_db as kb

    line = f"Board db `{db_path}`: {detail}"
    row = conn.execute(
        "SELECT id, status, body, created_at FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (KEY,),
    ).fetchone()
    if row and row[1] not in ("done", "failed", "cancelled"):
        since = int(time.time()) - _COMMENT_EVERY
        recent = (f"`{db_path}`" in (row[2] or "") and (row[3] or 0) >= since) or conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND body LIKE ? AND created_at >= ? LIMIT 1",
            (row[0], AUTHOR, f"%`{db_path}`%", since),
        ).fetchone()
        if not recent:
            kb.add_comment(conn, row[0], AUTHOR, f"Again: {line}")
        return row[0]
    body = (
        f"Kanban workspace/worktree GC could not read a board, so it kept every workspace it "
        f"could not prove unused (fail-closed) and finished-task workspaces are piling up.\n\n{line}\n\n"
        "Fix: if the path is not a real board (a stray or empty kanban.db outside a valid board slug "
        "folder), move it to ~/.hermes/archive/; if it is a real board, repair it with "
        "`hermes kanban --board <slug> recover`. Then confirm agent.log has no new "
        "'Kanban worktree sweep aborted' / 'cannot tell whether another task still uses' lines."
    )
    tid = kb.create_task(
        conn, title=f"Kanban workspace GC blocked: cannot read {db_path}"[:120], body=body,
        assignee="default", created_by=AUTHOR, priority=50,
        idempotency_key=None if row else KEY,
    )
    if row:  # the previous card is finished: the key now names this one (newest wins)
        with kb.write_txn(conn, allow_nested=True):
            conn.execute("UPDATE tasks SET idempotency_key = ? WHERE id = ?", (KEY, tid))
    return tid
