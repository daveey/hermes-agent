"""Host-local scratch custody surviving foreign task eviction; never synced.

Custody establishes a path, not deletion authority. Fresh Board terminal evidence
supplies retention, while local pickup and the receipt are fenced at rename.

Two removers share the same guards: the scheduled gc_custody (retention age, in-process
archive) and a worker's disk-pressure adapter through pressure_candidates /
terminal_owner / removal_guard / release (no retention age; the adapter bounds and
isolates archive creation and always removes through remove_archived).
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
    """Inside eviction's transaction: explicit scratch paths, or the default ``<root>/<id>`` tree when the
    row's path is NULL and that tree exists (it is the only place the task's scratch can be)."""
    row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row["workspace_kind"] != "scratch":
        return
    if row["workspace_path"]:
        path = Path(row["workspace_path"]).expanduser()
    else:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_worktree_gc import _board_slug
        path = kb.workspaces_root(board=_board_slug(conn)) / task_id
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
    # Local session history only, never a board DB; callers supply _state_paths().
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)  # noqa: kanban-lockguard
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


def _num(value):
    return type(value) in (int, float) and value > 0


def _terminal(hub, board, task_id, runs=()):
    """Fresh Board terminal evidence, or None. The returned row carries ``retention_at``: its
    ``completed_at``, or for an ``archived`` row the Board never completed (``completed_at`` null) the
    newest of the row's ``updated_at`` (ms) and the latest ended run in *runs*."""
    evidence = hub.row_evidence(board, "tasks", task_id)
    row = evidence.get("row")
    if (evidence.get("table") != "tasks" or evidence.get("uid") != task_id
            or type(evidence.get("head")) is not int or evidence.get("state") != "live"
            or not isinstance(row, dict) or row.get("uid") != task_id
            or row.get("status") not in ("done", "archived")
            or "lease_holder" not in row or "lease_expires" not in row):
        return None
    if _num(row.get("completed_at")):
        retention_at = row["completed_at"]
    elif row["status"] == "archived" and row.get("completed_at") is None:
        clocks = [row["updated_at"] / 1000] if _num(row.get("updated_at")) else []
        clocks += [r["ended_at"] for r in runs if _num(r.get("ended_at"))]
        if not clocks:
            return None
        retention_at = max(clocks)
    else:
        return None
    if row["lease_holder"] is not None and (not row["lease_expires"] or row["lease_expires"] > time.time() * 1000):
        return None
    return dict(row, retention_at=retention_at)


def _legacy_unchanged(receipt):
    evidence = json.loads(receipt["evidence"])
    if evidence["kind"] != "legacy":
        return True
    matches = _legacy_candidates(evidence["board"]).get(receipt["task_id"], [])
    return matches == [(Path(evidence["state_db"]), evidence["session"], evidence["runs"][0]["id"])]


def _safe(conn, receipt, path, *, tree=None):
    tid = receipt["task_id"]
    current = conn.execute(f"SELECT * FROM {_TABLE} WHERE task_id=?", (tid,)).fetchone()
    if current is None or dict(current) != receipt or conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        return False
    tree = path if tree is None else tree
    if tree.is_symlink() or not tree.is_dir() or not ws._is_managed_scratch_path(path):
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
    return (_sessions_idle(path) and _sessions_idle(tree)
            and not defer_owned_job_cleanup(tid, tree))


class _DetachUncommitted(Exception):
    """The tree was renamed aside but the receipt DELETE did not cleanly commit: stop the pass."""


def _runs_ended(receipt):
    runs = json.loads(receipt["evidence"])["runs"]
    return bool(runs) and all(r["ended_at"] for r in runs)


def _pressure_candidate(receipt):
    """Local-only scheduling view of one receipt, or None when gc_custody would skip it before the Board."""
    try:
        if not _runs_ended(receipt):
            return None
        ended = max(r["ended_at"] for r in json.loads(receipt["evidence"])["runs"])
        path = Path(receipt["workspace_path"])
        # removal_guard (_safe) always refuses a tree outside managed scratch storage: never list one,
        # or a pressure reclaimer builds a full archive before finding out.
        if (type(ended) not in (int, float) or ended <= 0 or not path.is_absolute()
                or path.is_symlink() or not path.is_dir() or not ws._is_managed_scratch_path(path)):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"task_id": receipt["task_id"], "workspace_path": receipt["workspace_path"],
            "finished_at": ended, "receipt": dict(receipt)}


def pressure_candidates(conn, limit=1000):
    """Evicted-owner trees a disk-pressure reclaimer may try: local reads only, no Board call, no write.

    One dict per receipt whose tree is a directory and whose recorded runs all ended:
    ``{task_id, workspace_path, finished_at, receipt}``, oldest ``finished_at`` (latest recorded run end,
    scheduling only) first, at most ``limit``. Listing grants nothing: a caller needs ``terminal_owner()``
    evidence, a verified complete archive and ``kanban_scratch_archive.remove_archived(conn, path, archive,
    removal_guard(conn, receipt), release(conn, receipt))``; gc_custody's retention age does not apply.
    """
    out = [c for c in map(_pressure_candidate, receipts(conn)) if c is not None]
    out.sort(key=lambda c: (c["finished_at"], c["task_id"]))
    return out[:limit]


def pressure_candidate(conn, task_id):
    """The current pressure_candidates() entry for task_id, or None (receipt gone, run live, tree missing)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (_TABLE,)).fetchone():
        return None
    row = conn.execute(f"SELECT * FROM {_TABLE} WHERE task_id=?", (task_id,)).fetchone()
    return _pressure_candidate(dict(row)) if row is not None else None


def terminal_owner(hub, board, receipt):
    """Fresh Board terminal evidence for an evicted owner, or None; gc_custody's own rule (runs ended, _terminal).

    Network: never call inside a DB transaction. A malformed receipt (unreadable evidence or runs) is None,
    checked before any Board read; Board transport errors propagate (the caller keeps the tree either way).
    """
    try:
        if not _runs_ended(receipt):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return _terminal(hub, board, receipt["task_id"], runs=json.loads(receipt["evidence"])["runs"])


def removal_guard(conn, receipt):
    """remove_archived guard(tree): gc_custody's _safe for exactly this receipt (unchanged, no local row, idle)."""
    path = Path(receipt["workspace_path"])
    return lambda tree: _safe(conn, receipt, path, tree=tree)


def release(conn, receipt):
    """remove_archived finalize: consume exactly this receipt inside the detach fence, else raise (rolls back)."""
    def finalize():
        from hermes_cli.kanban_scratch_archive import ArchiveRefused
        cur = conn.execute(f"DELETE FROM {_TABLE} WHERE task_id=? AND workspace_path=? AND evidence=?",
                           (receipt["task_id"], receipt["workspace_path"], receipt["evidence"]))
        if cur.rowcount != 1:
            raise ArchiveRefused("custody receipt changed before release")
    return finalize


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


def _remove(conn, receipt, owner, days, board, refusals=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_scratch_archive as sa
    tid, path = receipt["task_id"], Path(receipt["workspace_path"])
    since = owner["retention_at"]  # completed_at, or the archived-without-completion clock (_terminal)
    eligible_at = since + days * 86400
    log.info("Scratch custody: task=%s path=%s verified_terminal=%s eligible_at=%s",
             tid, path, owner["status"], eligible_at)
    if days <= 0 or eligible_at > time.time() or not _safe(conn, receipt, path):
        return 0
    before = path.stat()
    archive = None
    if not ws._scratch_tree_removable(path):
        key = f"custody:{tid}"
        if refusals is not None:
            from hermes_cli.kanban_scratch_backoff import skip_logged
            if skip_logged(refusals, key, path, since, tid):
                return 0
        try:
            archive = sa.archive_workspace(path, kb.kanban_home() / "archive" / "scratch" / board / tid,
                                           task_id=tid, completed_at=since)
        except (OSError, ValueError, sa.ArchiveRefused, subprocess.SubprocessError) as exc:
            if refusals is not None:
                refusals.record(key, path, since, str(exc))
            raise
        if refusals is not None:
            refusals.clear(key)
    if archive is not None:
        ws.release_lsp_clients(str(path))

        return sa.remove_archived(conn, path, archive, removal_guard(conn, receipt), release(conn, receipt))
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


def gc_custody(conn, days, *, budget=None, refusals=None):
    """Scheduled scratch GC backstop; authority reads never run in a write transaction.

    *budget* / *refusals* (``kanban_scratch_backoff``) bound the pass: once the budget
    is spent the remaining receipts wait for the next pass, untouched."""
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_worktree_gc import _board_slug
    from hermes_cli.kanban_scratch_archive import ArchiveRefused
    from hermes_cli.kanban_scratch_preserve import preserved
    if conn.in_transaction:
        return 0
    board = _board_slug(conn)
    removed = 0
    try:
        discover_legacy(conn, board)
        from hermes_cli import kanban_scratch_custody_discovery as discovery
        unowned = discovery.candidates(conn, board)
        rows = receipts(conn)
        if not rows and not unowned:
            return 0
        hub = client.Hub(client.load_identity())
        if unowned:
            try:
                discovery.discover(conn, board, hub, unowned)
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, client.HubError,
                    client.SyncUnavailable, client.Resync) as exc:
                # Fail closed: undiscovered trees stay owner_unverified; existing receipts still run.
                log.warning("Scratch custody discovery stopped: board evidence unavailable (%s)", exc)
            rows = receipts(conn)
        for receipt in rows:
            try:
                if preserved(receipt["task_id"], receipt["workspace_path"]):
                    log.info("Scratch custody preserved: task=%s path=%s reason=scratch_gc_preserve",
                             receipt["task_id"], receipt["workspace_path"])
                    continue
                if not Path(receipt["workspace_path"]).is_dir():
                    # Nothing to reclaim: skip before any Board call; the receipt stays as evidence.
                    log.info("Scratch custody skipped: task=%s path=%s reason=path_unavailable",
                             receipt["task_id"], receipt["workspace_path"])
                    continue
                if not _runs_ended(receipt):
                    log.warning("Scratch custody preserved: task=%s path=%s reason=run_unverified "
                                "eligible_at=None", receipt["task_id"], receipt["workspace_path"])
                    continue
                if budget is not None and budget.exhausted():
                    budget.defer()
                    continue
                try:
                    owner = _terminal(hub, board, receipt["task_id"],
                                      runs=json.loads(receipt["evidence"])["runs"])
                except (OSError, client.SyncUnavailable, client.Resync) as exc:
                    # Unavailable authority: stop rather than repeat the failure for every receipt.
                    log.warning("Scratch custody preserved: task=%s path=%s reason=board_unavailable "
                                "eligible_at=None; pass stopped (%s)", receipt["task_id"],
                                receipt["workspace_path"], exc)
                    break
                if owner:
                    removed += _remove(conn, receipt, owner, days, board, refusals)
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
