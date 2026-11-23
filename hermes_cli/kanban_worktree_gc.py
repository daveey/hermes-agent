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
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from hermes_cli import kanban_db_workspace as ws
from hermes_cli import kanban_lockguard
from hermes_cli.kanban_workspace_jobs import defer_owned_job_cleanup

log = logging.getLogger(__name__)


def retention_days() -> float:
    """Age-based GC window in days; 0 disables it.

    ``kanban.worktree_retention_hours`` (a non-negative number), when set,
    takes precedence over the whole-day ``kanban.worktree_retention_days`` and
    is returned as a fraction of a day. An invalid value of the key in effect
    disables age-based GC and logs a warning; it never falls back to the other
    key. [daveey]
    """
    from hermes_cli.config import load_config
    try:
        kanban = load_config().get("kanban", {})
        hours = kanban.get("worktree_retention_hours")
        if hours is not None:
            if (isinstance(hours, bool) or not isinstance(hours, (int, float))
                    or not math.isfinite(hours) or hours < 0):
                raise ValueError("invalid worktree_retention_hours")
            return hours / 24
        value = kanban.get("worktree_retention_days", 7)
        if isinstance(value, bool) or int(value) != value or value < 0:
            raise ValueError("invalid worktree retention")
        return int(value)
    except Exception:
        log.warning("Cannot read worktree retention; age-based GC disabled", exc_info=True)
        return 0


def gc_task_scratch(conn: sqlite3.Connection, *, older_than_days: float | None = None,
                    budget=None) -> int:
    """Retry terminal scratch cleanup, failing closed on any unreadable board.

    Only task records establish ownership; never infer it from directory names.
    Read every board before the first removal, even if an earlier board reports
    a live sharer. The owner is re-checked (unchanged, terminal, no active
    children) in a short write txn right before each removal, which detaches
    the tree by rename there; walks and rmtree never hold the board lock.

    A tree that is kept only because it holds standalone git clones is archived
    and then removed once its card finished more than *older_than_days* ago
    (default ``retention_days()``: ``kanban.worktree_retention_hours`` / 24 when
    set, else ``kanban.worktree_retention_days``; 0 disables). [daveey]

    *budget* (a ``kanban_scratch_backoff.Budget``; default unlimited) stops starting
    new archives once spent; archive refusals back off per tree across passes.
    """
    import shutil

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_scratch_preserve import preserved

    days = retention_days() if older_than_days is None else older_than_days
    removed = 0
    aged: list[tuple[str, Path, int | None]] = []
    # Only the candidate SELECT holds the board's write lock. The inventory, the tree walks and
    # every rmtree run outside it (they took 11 min for 29 trees on a loaded Mac, holding BEGIN
    # IMMEDIATE on the board throughout); each removal re-checks its owner in a short write txn
    # and detaches the tree by rename there, so a card resumed mid-GC never sees it half-deleted.
    try:
        with kb.write_txn(conn):
            rows = conn.execute(
                "SELECT t.id, t.workspace_path, t.workspace_kind, t.completed_at, t.created_at, "
                "(SELECT MAX(created_at) FROM task_events e WHERE e.task_id=t.id "
                " AND e.kind NOT LIKE 'workspace%') AS last_event "
                "FROM tasks t WHERE t.workspace_kind='scratch' "
                f"AND t.status IN {ws._TERMINAL_STATUSES_SQL}"
            ).fetchall()
        known_paths = _scratch_owner_paths(conn)
        for board in ws._sibling_board_db_files(conn):
            with _read_board(board) as other:
                known_paths.update(_scratch_owner_paths(other))
    except (OSError, sqlite3.Error):
        log.warning("Scratch GC aborted: cannot read all boards; preserving workspaces", exc_info=True)
        return 0
    _sweep_detached_trash(kb.workspaces_root(), rows)
    try:
        _report_unowned_scratch(kb.workspaces_root(), known_paths)
    except OSError:
        log.warning("Scratch preservation inventory unavailable; unknown paths remain preserved",
                    exc_info=True)
    for row in rows:
        path = Path(row["workspace_path"] or (kb.workspaces_root() / row["id"])).expanduser()
        if preserved(row["id"], path):
            continue  # kanban.scratch_gc_preserve: never walked, archived or removed
        if not path.is_dir() or path.is_symlink() or not ws._is_managed_scratch_path(path):
            continue
        if ws._has_active_children(conn, row["id"]):
            continue
        removable = ws._scratch_tree_removable(path)
        if not removable and not _finished_before(row, days):
            _report_scratch_retention(row, path, days)
            continue
        trash = None
        with kb.write_txn(conn):
            if not _scratch_owner_unchanged(conn, row, path):
                continue
            if ws._defer_shared_workspace_cleanup(conn, row["id"], path):
                continue
            if defer_owned_job_cleanup(row["id"], path):
                continue
            if not removable:
                aged.append((row["id"], path, row["completed_at"]))
                continue
            ws.release_lsp_clients(str(path))
            trash = path.with_name(f".{path.name}{_TRASH_INFIX}{uuid.uuid4().hex}")
            try:
                os.rename(path, trash)
            except OSError:
                log.warning("Scratch GC could not detach %s; keeping it", path, exc_info=True)
                continue
            ws._log_scratch_removal(conn, row["id"], path, "scratch workspace (GC retry)")
            removed += 1
        shutil.rmtree(trash, ignore_errors=True)
        if os.path.lexists(trash):
            log.warning("Scratch GC left detached tree %s (from %s) after a partial removal", trash, path)
    # Archiving runs git for minutes on big clones: never hold the board's write lock for it.
    # Oldest first; a tree refused recently waits out its backoff, and once the pass budget is
    # spent the rest wait for the next pass (kanban_scratch_backoff), so the pass ends in time.
    from hermes_cli.kanban_scratch_backoff import Budget, Refusals
    budget = budget if budget is not None else Budget()
    refusals = Refusals(_board_slug(conn))
    for task_id, path, completed_at in sorted(aged, key=lambda a: (a[2] or 0, a[0])):
        if budget.exhausted():
            budget.defer()
            continue
        removed += _archive_and_remove_scratch(conn, task_id, path, completed_at, refusals)
    from hermes_cli.kanban_scratch_custody import gc_custody
    removed += gc_custody(conn, days, budget=budget, refusals=refusals)
    budget.report("aged scratch tree(s) or custody receipt(s)")
    return removed


# A tree detached for removal: ``.<workspace name>.gc-rm-<uuid4 hex>``, a sibling of the workspace.
# Only this GC creates the name, after the owner check passed; the rename runs before the removal
# is logged, so a log failure rolls the txn back but leaves the tree detached with no removal
# record. _sweep_detached_trash therefore logs every tree it deletes.
_TRASH_INFIX = ".gc-rm-"
_TRASH_NAME = re.compile(r"\..+\.gc-rm-[0-9a-f]{32}")


def _is_detached_trash(path: Path) -> bool:
    return _TRASH_NAME.fullmatch(path.name) is not None


def _sweep_detached_trash(root: Path, rows) -> None:
    """Finish removals an earlier run detached but could not rmtree (no board lock is held here).

    Looks in the workspaces root and beside each candidate's workspace, never follows a symlink,
    and only touches names this GC mints."""
    import shutil

    from hermes_cli.kanban_scratch_preserve import preserved

    parents = {root}
    for row in rows:
        if row["workspace_path"]:
            path = Path(row["workspace_path"]).expanduser()
            if ws._is_managed_scratch_path(path):
                parents.add(path.parent)
    for parent in parents:
        try:
            entries = [p for p in parent.iterdir() if _is_detached_trash(p)]
        except OSError:
            continue
        for trash in entries:
            if trash.is_symlink() or not trash.is_dir():
                continue
            if preserved(None, trash):
                log.info("Scratch preserved: reason=scratch_gc_preserve path=%s", trash)
                continue
            shutil.rmtree(trash, ignore_errors=True)
            if os.path.lexists(trash):
                log.warning("Scratch GC left detached tree %s; retried next run", trash)
            else:
                log.warning("Scratch GC swept detached tree %s left by an earlier run", trash)


def _scratch_owner_unchanged(conn: sqlite3.Connection, row: sqlite3.Row, path: Path) -> bool:
    """Inside the removal txn: the candidate's task still owns ``path`` exactly as selected, is
    still terminal and has no active children (it may have been resumed or reassigned since)."""
    from hermes_cli import kanban_db as kb
    current = conn.execute("SELECT workspace_kind, workspace_path, completed_at FROM tasks WHERE id=?",
                           (row["id"],)).fetchone()
    if current is None or current["workspace_kind"] != "scratch":
        return False
    if (current["workspace_path"], current["completed_at"]) != (row["workspace_path"], row["completed_at"]):
        return False
    if Path(current["workspace_path"] or (kb.workspaces_root() / row["id"])).expanduser() != path:
        return False
    return ws._is_terminal_task(conn, row["id"]) and not ws._has_active_children(conn, row["id"])


def _scratch_owner_paths(conn: sqlite3.Connection) -> set[Path]:
    """Read local records only; this inventory never supplies deletion candidates."""
    from hermes_cli import kanban_db as kb

    root = kb.workspaces_root(board=_board_slug(conn))
    paths = set()
    from hermes_cli.kanban_scratch_custody import receipts
    paths.update(Path(r["workspace_path"]).resolve() for r in receipts(conn))
    for row in conn.execute("SELECT id, workspace_kind, workspace_path FROM tasks"):
        if row["workspace_path"]:
            paths.add(Path(row["workspace_path"]).expanduser().resolve())
        elif row["workspace_kind"] == "scratch":
            paths.add((root / row["id"]).resolve())
    return paths


def _report_unowned_scratch(root: Path, known_paths: set[Path]) -> None:
    """Observe root children without entering them or guessing task IDs from names."""
    if not root.exists():
        return
    root_key = root.resolve()
    represented = {root_key / known.relative_to(root_key).parts[0]
                   for known in known_paths if known.is_relative_to(root_key) and known != root_key}
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir() or _is_detached_trash(path):
            continue  # detached trash is swept by gc_task_scratch, not an unknown owner
        if path.resolve() not in represented:
            from hermes_cli.kanban_scratch_preserve import preserved
            if preserved(path.name, path):
                log.info("Scratch preserved: reason=scratch_gc_preserve path=%s", path)
                continue
            log.warning("Scratch preserved: reason=owner_unverified path=%s; "
                        "no local task workspace record; moved or unknown ownership; "
                        "no deletion deadline", path)


def _report_scratch_retention(row: sqlite3.Row, path: Path, days: float) -> None:
    # Logs, not task events: observing retention must never extend its age.
    since = row["completed_at"] or row["last_event"] or row["created_at"]
    reason = "retention_disabled" if days <= 0 else "retention_pending"
    eligible_at = since + days * 86400 if days > 0 and since else None
    log.warning("Scratch preserved: reason=%s path=%s task=%s eligible_at=%s; "
                "deadline is archival eligibility only, all safety guards still apply",
                reason, path, row["id"], eligible_at)


def _finished_before(row: sqlite3.Row, days: float) -> bool:
    if days <= 0:
        return False
    since = row["completed_at"] or row["last_event"] or row["created_at"]
    return bool(since and since <= time.time() - days * 86400)


def _board_slug(conn: sqlite3.Connection) -> str:
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import board_slug_for_path
    db_file = _main_db_file(conn)
    return board_slug_for_path(db_file) if db_file else "default"


def _archive_and_remove_scratch(conn: sqlite3.Connection, task_id: str, path: Path,
                                completed_at: int | None, refusals=None) -> int:
    """Archive every git repo in an aged scratch tree, then remove it; 1 when removed.

    Any refusal or failure keeps the workspace and logs why; with *refusals*
    (``kanban_scratch_backoff.Refusals``) an archive refusal is remembered and the
    tree is not re-archived until its backoff passes or it changes. The owner is
    re-checked under the write lock after archiving, and the tree is renamed
    away inside that lock so a resumed card can never see it half-deleted.
    """
    import subprocess

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_scratch_archive as sa

    owner = conn.execute("SELECT workspace_kind, workspace_path, completed_at FROM tasks WHERE id=?",
                         (task_id,)).fetchone()

    def _row_path(row):
        # Same expression as candidate selection; raw-row equality below still fences mutation.
        return Path(row["workspace_path"] or (kb.workspaces_root() / task_id)).expanduser()

    def guard(tree):
        current = conn.execute("SELECT workspace_kind, workspace_path, completed_at FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
        return (owner is not None and current is not None and tuple(current) == tuple(owner)
                and owner["workspace_kind"] == "scratch" and _row_path(owner) == path
                and owner["completed_at"] == completed_at
                and ws._is_terminal_task(conn, task_id) and not ws._has_active_children(conn, task_id)
                and not ws._defer_shared_workspace_cleanup(conn, task_id, path)
                and not defer_owned_job_cleanup(task_id, tree))

    def owner_unchanged():
        # Re-checked right before the archive changes the tree (foreign ownership adoption).
        with kb.write_txn(conn):
            return guard(path)

    archive_dir = kb.kanban_home() / "archive" / "scratch" / _board_slug(conn) / task_id
    if refusals is not None:
        from hermes_cli.kanban_scratch_backoff import skip_logged
        if skip_logged(refusals, task_id, path, completed_at, task_id):
            return 0
    try:
        final = sa.archive_workspace(path, archive_dir, task_id=task_id, completed_at=completed_at,
                                     owner_check=owner_unchanged)
    except (sa.ArchiveRefused, OSError, subprocess.SubprocessError, ValueError) as exc:
        log.info("Keeping aged scratch workspace for task %s: %s (%s)", task_id, path, exc)
        entry = None
        if refusals is not None:
            refusals.record(task_id, path, completed_at, str(exc))
            entry = refusals.entries.get(task_id)
        from hermes_cli.kanban_scratch_backoff import report_archive_refused
        report_archive_refused(path, task_id, entry, exc)
        return 0
    if refusals is not None:
        refusals.clear(task_id)
    ws.release_lsp_clients(str(path))

    def finalize():
        kb._append_event(conn, task_id, "workspace_archived", {"path": str(path), "archive": str(final)})
        ws._log_scratch_removal(conn, task_id, path, "scratch workspace (aged, git archived)")

    try:
        return sa.remove_archived(conn, path, final, guard, finalize)
    except (sa.ArchiveRefused, OSError) as exc:
        log.info("Keeping archived scratch workspace for task %s (or detached recovery tree): %s (%s)",
                 task_id, path, exc)
        from hermes_cli.kanban_scratch_backoff import keep_detail
        log.warning("Scratch preserved: reason=detach_refused path=%s task=%s; %s", path, task_id, keep_detail(exc))
        return 0


def gc_task_worktrees(conn: sqlite3.Connection, *, older_than_days: float = 7,
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
        conn = kanban_lockguard.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    except sqlite3.OperationalError:
        conn = kanban_lockguard.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
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
    except _BoardError as exc:
        log.warning("Kanban worktree sweep aborted on board db %s; preserving uncertain worktrees",
                    exc.board, exc_info=True)
        from hermes_cli import kanban_gc_alert
        kanban_gc_alert.report(None, str(exc.board), f"startup worktree sweep aborted: {exc.__cause__!r}")
    except Exception:
        log.warning("Kanban worktree sweep aborted; preserving uncertain worktrees", exc_info=True)


class _BoardError(Exception):
    """A board db failed during the sweep; names it for the GC-abort card. [daveey]"""

    def __init__(self, board: Path):
        super().__init__(str(board))
        self.board = board


def _sweep_repo(repo_root: str) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_constants import get_hermes_home
    days = retention_days()
    if not days:
        return
    # Use the shared enumerator without opening/creating the selected board.
    with contextlib.closing(kanban_lockguard.connect(":memory:")) as empty:
        boards = ws._sibling_board_db_files(empty)
    selected = kb.kanban_db_path().resolve()
    if selected.is_file() and selected not in boards:
        boards.append(selected)
    known_paths: set[str] = set()
    # Read ALL boards before any mutation: corruption must not look like deletion.
    for board in boards:
        try:
            with _read_board(board) as conn:
                for row in conn.execute("SELECT workspace_path FROM tasks WHERE workspace_path IS NOT NULL"):
                    known_paths.add(str(Path(row["workspace_path"]).expanduser().resolve()))
        except sqlite3.Error as exc:
            raise _BoardError(board) from exc
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
        try:
            with contextlib.closing(kanban_lockguard.connect(board.as_uri() + "?mode=rw", uri=True, timeout=1,
                                                   isolation_level=None)) as conn:
                conn.row_factory = sqlite3.Row
                gc_task_worktrees(conn, older_than_days=days, repo_root=repo_root)
        except sqlite3.OperationalError as exc:
            from hermes_cli.kanban_db_connect import _is_busy_error
            if not _is_busy_error(exc):
                raise _BoardError(board) from exc
            # Another writer held the lock past our short (TUI-foreground) wait:
            # normal contention, so skip this board and keep cleaning the rest.
            log.info("worktree GC skipped busy board %s; retrying next sweep", board)
            _note_busy_skip(board)
        else:
            _note_busy_skip(board, cleared=True)


BUSY_ALERT_AFTER = 3


def _note_busy_skip(board: Path, *, cleared: bool = False) -> None:
    """Count back-to-back busy skips per board; at BUSY_ALERT_AFTER open the GC card. [daveey]

    A board that is busy on every sweep would otherwise never be cleaned, silently.
    """
    from hermes_cli import kanban_db as kb
    state_file = kb.kanban_home() / "gc-busy-skips.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8-sig"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    key = str(board)
    if cleared:
        if key not in state:
            return
        state.pop(key)
        count = 0
    else:
        prior = state.get(key)
        count = (prior if isinstance(prior, int) and prior > 0 else 0) + 1
        state[key] = count
    try:
        tmp = state_file.with_name(f".{state_file.name}.{os.getpid()}")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(state_file)
    except OSError:
        log.info("worktree GC could not record busy-skip count in %s", state_file, exc_info=True)
    if count >= BUSY_ALERT_AFTER:
        from hermes_cli import kanban_gc_alert
        kanban_gc_alert.report(None, key, f"worktree GC skipped this board {count} sweeps in a row "
                                          "because it stayed busy (database is locked); its finished "
                                          "worktrees are not being cleaned")
