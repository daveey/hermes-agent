"""Host-local scratch custody surviving foreign task eviction; never synced.

Custody establishes a path, not deletion authority. Fresh Board terminal evidence
supplies retention, while local pickup and the receipt are fenced at rename.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from hermes_cli import kanban_db_workspace as ws

log = logging.getLogger(__name__)
_TABLE = "kanban_scratch_custody"


def _ensure(conn):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                 "task_id TEXT PRIMARY KEY, workspace_path TEXT NOT NULL, evidence TEXT NOT NULL)")


def receipts(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (_TABLE,)).fetchone():
        return []
    return [dict(r) for r in conn.execute(f"SELECT * FROM {_TABLE}")]


def retain(conn, task_id):
    """Inside eviction's transaction: only explicit scratch paths, never defaults."""
    row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return
    path = Path(row["workspace_path"]).expanduser()
    # A missing or non-directory path has nothing to retain: skip rather than record it.
    if not path.is_absolute() or path.is_symlink() or not path.is_dir() or not ws._is_managed_scratch_path(path):
        return
    runs = [dict(r) for r in conn.execute(
        "SELECT id, ended_at FROM task_runs WHERE task_id=? ORDER BY id", (task_id,))]
    _ensure(conn)
    conn.execute(f"INSERT OR REPLACE INTO {_TABLE} VALUES (?, ?, ?)",
                 (task_id, str(path), json.dumps({"kind": "eviction", "runs": runs}, sort_keys=True)))


def _state_paths():
    from hermes_cli import kanban_db as kb
    root = kb.kanban_home()
    # The original local stores, never the Board's synced session history.
    paths = [root / "state.db"]
    profiles = root / "profiles"
    if profiles.exists():
        paths.extend(p / "state.db" for p in profiles.iterdir() if (p / "state.db").exists())
    return paths


@contextlib.contextmanager
def _state(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _sessions_idle(path):
    """A live same-tree session, admission or ancestor turn lease vetoes removal."""
    for db in _state_paths():
        with _state(db) as state:
            for session in state.execute("SELECT id, cwd, ended_at FROM sessions WHERE cwd IS NOT NULL"):
                cwd = Path(session["cwd"]).expanduser().resolve()
                target = path.resolve()
                if not cwd.is_relative_to(target):
                    continue
                if session["ended_at"] is None:
                    return False
                if state.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? "
                                 "AND status != 'terminal'", (session["id"],)).fetchone():
                    return False
                if state.execute(
                    "WITH RECURSIVE lineage(id) AS (SELECT ? UNION SELECT s.parent_session_id "
                    "FROM sessions s JOIN lineage l ON s.id=l.id WHERE s.parent_session_id IS NOT NULL) "
                    "SELECT 1 FROM session_turn_leases WHERE conversation_id IN (SELECT id FROM lineage) "
                    "AND expires_at > ?", (session["id"], time.time())).fetchone():
                    return False
    return True


def _legacy_candidates(board):
    candidates = {}
    for db in _state_paths():
        with _state(db) as state:
            rows = state.execute(
                "SELECT a.request_id, a.target_session_id, a.status, s.source, s.cwd, s.ended_at "
                "FROM session_admissions a LEFT JOIN sessions s ON s.id=a.target_session_id "
                "WHERE a.request_id LIKE 'kanban:%'").fetchall()
            for row in rows:
                try:
                    identity = json.loads(row["request_id"].removeprefix("kanban:"))
                except ValueError:
                    continue
                if (not isinstance(identity, list) or len(identity) != 3 or identity[0] != board
                        or not isinstance(identity[1], str) or type(identity[2]) is not int):
                    continue
                candidates.setdefault(identity[1], []).append((db, dict(row), identity[2]))
    return candidates


def discover_legacy(conn, board):
    """Recover only exact local request -> ended session -> local run tombstone links."""
    from hermes_cli.kanban_db_connect import write_txn
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_ended_run_tombstones'").fetchone():
        return
    candidates = _legacy_candidates(board)
    for task_id, matches in candidates.items():
        # Duplicate admissions/targets are ambiguous, even if one happens to be ended.
        if len(matches) != 1:
            continue
        db, row, run_id = matches[0]
        if row["source"] != "kanban" or row["status"] != "terminal" or not row["ended_at"] or not row["cwd"]:
            continue
        path = Path(row["cwd"])
        if not path.is_absolute() or path.is_symlink() or not path.is_dir() or not ws._is_managed_scratch_path(path):
            continue
        # Only the task's own default tree (<root>/<task_id>): a dir-kind sharer's session runs in
        # its owner's scratch, and that tree is not this task's to reclaim.
        if path.name != task_id or ws._is_managed_scratch_path(path.parent):
            continue
        if not _sessions_idle(path):
            continue
        with write_txn(conn):
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                continue
            ended = conn.execute("SELECT ended_at FROM kanban_ended_run_tombstones "
                                 "WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone()
            if ended is None or not ended[0]:
                continue
            _ensure(conn)
            evidence = {"kind": "legacy", "board": board, "state_db": str(db), "session": row,
                        "runs": [{"id": run_id, "ended_at": ended[0]}]}
            conn.execute(f"INSERT OR IGNORE INTO {_TABLE} VALUES (?, ?, ?)",
                         (task_id, str(path), json.dumps(evidence, sort_keys=True)))


def _terminal(hub, board, task_id):
    evidence = hub.row_evidence(board, "tasks", task_id)
    row = evidence.get("row")
    if (evidence.get("table") != "tasks" or evidence.get("uid") != task_id
            or type(evidence.get("head")) is not int or evidence.get("state") != "live"
            or not isinstance(row, dict) or row.get("uid") != task_id
            or row.get("status") not in ("done", "archived")
            or type(row.get("completed_at")) not in (int, float) or row["completed_at"] <= 0
            or "lease_holder" not in row or "lease_expires" not in row):
        return None
    if row["lease_holder"] is not None and (not row["lease_expires"] or row["lease_expires"] > time.time() * 1000):
        return None
    return row


def _legacy_unchanged(receipt):
    evidence = json.loads(receipt["evidence"])
    if evidence["kind"] != "legacy":
        return True
    matches = _legacy_candidates(evidence["board"]).get(receipt["task_id"], [])
    return matches == [(Path(evidence["state_db"]), evidence["session"], evidence["runs"][0]["id"])]


def _safe(conn, receipt, path):
    tid = receipt["task_id"]
    current = conn.execute(f"SELECT * FROM {_TABLE} WHERE task_id=?", (tid,)).fetchone()
    if current is None or dict(current) != receipt or conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        return False
    if path.is_symlink() or not path.is_dir() or not ws._is_managed_scratch_path(path):
        return False
    if ws._has_active_children(conn, tid):
        return False
    if not _legacy_unchanged(receipt):
        return False
    for other in receipts(conn):
        if other["task_id"] == tid:
            continue
        other_path = Path(other["workspace_path"]).resolve()
        if other_path.is_relative_to(path.resolve()) or path.resolve().is_relative_to(other_path):
            return False
    # No task-event writes for an evicted owner (including deferred-shared events).
    if ws._workspace_in_use_by_other(conn, tid, path) is not None:
        return False
    from hermes_cli.kanban_workspace_jobs import defer_owned_job_cleanup
    return _sessions_idle(path) and not defer_owned_job_cleanup(tid, path)


class _DetachUncommitted(Exception):
    """The tree was renamed aside but the receipt DELETE did not cleanly commit: stop the pass."""


def _restore_detached(conn, tid, path, trash, exc):
    """After a failed commit, put the tree back unless something now occupies the path. Never deletes."""
    try:
        present = conn.execute(f"SELECT 1 FROM {_TABLE} WHERE task_id=?", (tid,)).fetchone() is not None
        receipt = "present" if present else "absent"
    except sqlite3.Error:
        receipt = "unknown"
    if os.path.lexists(path):
        # POSIX rename replaces an empty directory: never clobber a tree recreated after rollback.
        log.error("Scratch custody detach not committed: task=%s path=%s trash=%s receipt=%s "
                  "restored=False reason=path_reoccupied (%s)", tid, path, trash, receipt, exc)
        return
    try:
        trash.rename(path)
    except OSError as restore_exc:
        log.error("Scratch custody detach not committed: task=%s path=%s trash=%s receipt=%s "
                  "restored=False reason=restore_failed (%s; %s)", tid, path, trash, receipt, exc, restore_exc)
        return
    log.error("Scratch custody detach not committed: task=%s path=%s receipt=%s restored=True (%s)",
              tid, path, receipt, exc)


def _remove(conn, receipt, owner, days, board):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_scratch_archive as sa
    tid, path = receipt["task_id"], Path(receipt["workspace_path"])
    eligible_at = owner["completed_at"] + days * 86400
    log.info("Scratch custody: task=%s path=%s verified_terminal=%s eligible_at=%s",
             tid, path, owner["status"], eligible_at)
    if days <= 0 or eligible_at > time.time() or not _safe(conn, receipt, path):
        return 0
    before = path.stat()
    archive = None
    if not ws._scratch_tree_removable(path):
        archive = sa.archive_workspace(path, kb.kanban_home() / "archive" / "scratch" / board / tid,
                                       task_id=tid, completed_at=owner["completed_at"])
    # Remote resumption does not use this tree. Local pickup does, and shares this lock.
    trash = path.with_name(f".{path.name}.gc-{os.getpid()}")
    renamed = False
    try:
        with kb.write_txn(conn):
            if not _safe(conn, receipt, path):
                return 0
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                return 0
            if trash.exists():
                return 0
            # DELETE first: a SQL failure leaves the tree in place; a failed rename rolls the DELETE back.
            conn.execute(f"DELETE FROM {_TABLE} WHERE task_id=?", (tid,))
            ws.release_lsp_clients(str(path))
            path.rename(trash)
            renamed = True
    except Exception as exc:  # health: allow BLE001 -- restores a detached tree after any commit failure, then re-raises
        if not renamed:
            raise
        _restore_detached(conn, tid, path, trash, exc)
        raise _DetachUncommitted(str(exc)) from exc
    try:
        shutil.rmtree(trash)
    except OSError as exc:
        log.error("Scratch custody detached but not removed: task=%s path=%s trash=%s receipt=consumed (%s)",
                  tid, path, trash, exc)
        return 0
    log.info("Removed custody scratch: task=%s path=%s verified_terminal=%s eligible_at=%s archive=%s",
             tid, path, owner["status"], eligible_at, archive)
    return 1


def gc_custody(conn, days):
    """Scheduled scratch GC backstop; authority reads never run in a write transaction."""
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_worktree_gc import _board_slug
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    if conn.in_transaction:
        return 0
    board = _board_slug(conn)
    removed = 0
    try:
        discover_legacy(conn, board)
        rows = receipts(conn)
        if not rows:
            return 0
        hub = client.Hub(client.load_identity())
        for receipt in rows:
            try:
                if not Path(receipt["workspace_path"]).is_dir():
                    # Nothing to reclaim: skip before any Board call; the receipt stays as evidence.
                    log.info("Scratch custody skipped: task=%s path=%s reason=path_unavailable",
                             receipt["task_id"], receipt["workspace_path"])
                    continue
                runs = json.loads(receipt["evidence"])["runs"]
                if not runs or any(not r["ended_at"] for r in runs):
                    log.warning("Scratch custody preserved: task=%s path=%s reason=run_unverified "
                                "eligible_at=None", receipt["task_id"], receipt["workspace_path"])
                    continue
                try:
                    owner = _terminal(hub, board, receipt["task_id"])
                except (OSError, client.SyncUnavailable) as exc:
                    # Transport failure: every later receipt would wait out its own timeout.
                    log.warning("Scratch custody preserved: task=%s path=%s reason=board_unavailable "
                                "eligible_at=None; pass stopped (%s)", receipt["task_id"],
                                receipt["workspace_path"], exc)
                    break
                if owner:
                    removed += _remove(conn, receipt, owner, days, board)
                else:
                    log.warning("Scratch custody preserved: task=%s path=%s reason=terminal_unverified "
                                "eligible_at=None", receipt["task_id"], receipt["workspace_path"])
            except _DetachUncommitted:
                break  # logged with the tree's actual location; fail closed for the rest of the pass
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, client.HubError,
                    client.SyncUnavailable, ArchiveRefused, subprocess.SubprocessError) as exc:
                log.warning("Scratch custody preserved: task=%s path=%s reason=cleanup_unavailable "
                            "eligible_at=None (%s)", receipt["task_id"], receipt["workspace_path"], exc)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, client.HubError,
            client.SyncUnavailable, ArchiveRefused, subprocess.SubprocessError) as exc:
        log.warning("Scratch custody preserved: evidence or cleanup unavailable (%s)", exc)
    return removed
