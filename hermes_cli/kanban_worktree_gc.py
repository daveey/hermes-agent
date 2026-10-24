"""Retention-based task worktree GC, shared by kanban gc and CLI startup.

Clean committed work is saved under refs/hermes/kanban-archive/<task>/<oid>.
Dirty trees are never forced away. Missing owners are tombstoned, not deleted:
a missing board/task is not sufficient evidence that arbitrary work is disposable.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from hermes_cli import kanban_db_workspace as ws

log = logging.getLogger(__name__)


def retention_days() -> int:
    from hermes_cli.config import load_config
    try:
        value = load_config().get("kanban", {}).get("worktree_retention_days", 7)
        if isinstance(value, bool) or int(value) != value or value < 0:
            raise ValueError("invalid worktree retention")
        return int(value)
    except Exception:
        log.warning("Cannot read worktree retention; age-based GC disabled", exc_info=True)
        return 0


def gc_task_worktrees(conn: sqlite3.Connection, *, older_than_days: int = 7,
                     repo_root: str | None = None) -> int:
    """Reap terminal rows only, retaining active children and cross-board sharers.

    Persistent calendar timestamps intentionally use wall time, not process
    liveness timers. Zero disables archival, retaining legacy pushed-only GC.
    A write transaction prevents the owner being resumed during this sweep.
    """
    from hermes_cli import kanban_db as kb
    removed = 0
    with kb.write_txn(conn):
        rows = conn.execute(
            "SELECT t.*, (SELECT MAX(created_at) FROM task_events e WHERE e.task_id=t.id) AS last_event "
            "FROM tasks t WHERE workspace_kind='worktree' AND workspace_path IS NOT NULL "
            f"AND status IN {ws._TERMINAL_STATUSES_SQL}"
        ).fetchall()
        for row in rows:
            path = Path(row["workspace_path"]).expanduser()
            if not path.is_dir() or path.is_symlink():
                continue
            if repo_root is not None and ws._git_common_dir(path) != Path(repo_root).resolve() / ".git":
                continue
            if ws._has_active_children(conn, row["id"]):
                continue
            if ws._defer_shared_worktree_cleanup(conn, row["id"], str(path)):
                continue
            # Latest lifecycle evidence wins (e.g. reopened then archived again).
            since = max(row["completed_at"] or 0, row["last_event"] or 0, row["created_at"] or 0)
            old = bool(older_than_days > 0 and since and since <= time.time() - older_than_days * 86400)
            ws._cleanup_worktree_workspace(row["id"], str(path), row["branch_name"], archive_unpushed=old)
            removed += int(not path.exists())
    return removed


@contextlib.contextmanager
def _read_board(path: Path):
    """No creation, with WAL read-only fallback; an unreadable board aborts GC."""
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def sweep_repo(repo_root: str) -> None:
    """Startup backstop across all boards. Unknown task trees get durable tombstones.

    Enumerate registered worktrees, including outside .worktrees/. Do not guess
    ownership from a directory's age. No missing-owner tree is automatically
    removed; tombstones make it discoverable for explicit recovery/review.
    """
    try:
        _sweep_repo(repo_root)
    except Exception:
        log.warning("Kanban worktree sweep aborted; preserving uncertain worktrees", exc_info=True)


def _sweep_repo(repo_root: str) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_constants import get_hermes_home
    days = retention_days()
    if not days:
        return
    # Use the shared enumerator without opening/creating the selected board.
    with contextlib.closing(sqlite3.connect(":memory:")) as empty:
        boards = ws._sibling_board_db_files(empty)
    selected = kb.kanban_db_path().resolve()
    if selected.is_file() and selected not in boards:
        boards.append(selected)
    known_paths: set[str] = set()
    # Read ALL boards before any mutation: corruption must not look like deletion.
    for board in boards:
        with _read_board(board) as conn:
            for row in conn.execute("SELECT workspace_path FROM tasks WHERE workspace_path IS NOT NULL"):
                known_paths.add(str(Path(row["workspace_path"]).expanduser().resolve()))
    listing = ws._git(Path(repo_root), "worktree", "list", "--porcelain", timeout=10)
    if listing.returncode:
        return
    for line in listing.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.removeprefix("worktree "))
        if not re.fullmatch(r"t_[0-9a-f]+", path.name) or str(path.resolve()) in known_paths:
            continue
        # Tombstones are metadata outside the worktree; never alter unknown data.
        key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        tombstones = get_hermes_home() / "archive" / "kanban-orphans"
        tombstones.mkdir(parents=True, exist_ok=True)
        try:
            with (tombstones / f"{key}.json").open("x") as out:
                json.dump({"path": str(path), "repo": repo_root, "first_seen": time.time(),
                           "reason": "No owning task workspace found; preserved for manual recovery"}, out)
        except FileExistsError:
            pass
        log.warning("Orphan task worktree preserved and tombstoned: %s", path)
    for board in boards:
        # mode=rw refuses to fabricate a missing board after discovery.
        with contextlib.closing(sqlite3.connect(board.as_uri() + "?mode=rw", uri=True, timeout=1,
                                               isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            gc_task_worktrees(conn, older_than_days=days, repo_root=repo_root)
