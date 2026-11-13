"""Board-claim custody receipt discovery for scratch trees with no local row and no receipt. [daveey]

An eviction that predates custody receipts, or one whose row had ``workspace_path=NULL`` before
``retain()`` covered default paths, leaves a ``<root>/t_<16 hex>`` tree that no local record owns:
the retention GC only reports it as ``owner_unverified``, forever. Discovery never deletes. It
writes a ``{"kind": "board"}`` custody receipt, and only when the Board proves this machine had
custody of exactly that tree:

* the root child is named exactly ``t_[0-9a-f]{16}``, is a plain managed-scratch directory (not a
  symlink), is not pinned by ``kanban.scratch_gc_preserve``, and no local row (any board sharing
  storage) or receipt names it or a path inside or around it;
* the Board ``tasks`` row for that id is live, uid-matched and ``workspace_kind='scratch'``;
* the Board shows at least one ``claimed`` event for the task whose claim lock starts with this
  machine's hostname, and every run those claims name has ended on the Board.

The local Board replica only enumerates candidate claim events; each event and run is read back
from the Board itself. Removal then goes through the unchanged ``gc_custody`` path (terminal +
lease, retention, sessions idle, owned-job veto, nested receipts, archive-before-remove, rename
fence). A Board error, a missing row or a missing replica keeps the tree ``owner_unverified``.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db_workspace as ws

log = logging.getLogger(__name__)
_ID = re.compile(r"t_[0-9a-f]{16}")
MAX_PER_PASS = 50


def candidates(conn, board):
    """Root children discovery may look at: local reads only, no Board call, no write."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worktree_gc as gc
    from hermes_cli.kanban_scratch_preserve import preserved
    root = kb.workspaces_root(board=board)
    if not root.is_dir():
        return []
    known = gc._scratch_owner_paths(conn)
    for other in ws._sibling_board_db_files(conn):
        with gc._read_board(other) as sibling:
            known.update(gc._scratch_owner_paths(sibling))
    out = []
    for path in sorted(root.iterdir()):
        if not _ID.fullmatch(path.name) or not _plain(path) or preserved(path.name, path):
            continue
        resolved = path.resolve()
        if any(k == resolved or k.is_relative_to(resolved) or resolved.is_relative_to(k) for k in known):
            continue
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (path.name,)).fetchone():
            continue
        out.append(path)
        if len(out) >= MAX_PER_PASS:
            break
    return out


def _plain(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir() and ws._is_managed_scratch_path(path)


def _replica_claim_events(board, task_id):
    """Claimed-event uids the local Board replica lists for the task; None when it cannot be read."""
    from hermes_cli.kanban_generation import open_ro
    from hermes_cli.kanban_sync_client import shadow_path
    path = shadow_path(board)
    if not path.is_file():
        return None
    try:
        replica = open_ro(path)
        try:
            return [r[0] for r in replica.execute(
                "SELECT uid FROM task_events WHERE task_id=? AND kind='claimed' AND uid IS NOT NULL "
                "ORDER BY id", (task_id,))]
        finally:
            replica.close()
    except sqlite3.Error:
        return None


def _live(evidence, table, uid):
    row = evidence.get("row") if isinstance(evidence, dict) else None
    if (not isinstance(row, dict) or evidence.get("table") != table or evidence.get("uid") != uid
            or evidence.get("state") != "live" or row.get("uid") != uid):
        return None
    return row


def board_runs(hub, board, task_id):
    """``[{id: run_uid, ended_at}]`` for every run the Board shows this machine claimed, or None.

    None when the Board row is not a live scratch task, no claim is this machine's, or any such
    run has not ended. Network: never call inside a DB transaction; Board errors propagate."""
    from hermes_cli.kanban_host_identity import hostname_prefix
    row = _live(hub.row_evidence(board, "tasks", task_id), "tasks", task_id)
    if row is None or row.get("workspace_kind") != "scratch":
        return None
    events = _replica_claim_events(board, task_id)
    if not events:
        return None
    prefix = hostname_prefix()
    run_uids = []
    for uid in events:
        event = _live(hub.row_evidence(board, "task_events", uid), "task_events", uid)
        if event is None or event.get("task_id") != task_id or event.get("kind") != "claimed":
            continue
        try:
            lock = json.loads(event.get("payload") or "{}").get("lock")
        except (ValueError, AttributeError):
            continue
        run_uid = event.get("run_uid")
        if isinstance(lock, str) and lock.startswith(prefix) and isinstance(run_uid, str) and run_uid:
            if run_uid not in run_uids:
                run_uids.append(run_uid)
    if not run_uids:
        return None
    runs = []
    for uid in run_uids:
        run = _live(hub.row_evidence(board, "task_runs", uid), "task_runs", uid)
        if run is None or run.get("task_id") != task_id:
            return None
        ended = run.get("ended_at")
        if isinstance(ended, bool) or not isinstance(ended, (int, float)) or ended <= 0:
            return None
        runs.append({"id": uid, "ended_at": ended})
    return runs


def discover(conn, board, hub, paths):
    """Record a board custody receipt for each of *paths* the Board proves; returns how many."""
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_scratch_custody import _TABLE, _ensure
    from hermes_cli.kanban_scratch_preserve import preserved
    if conn.in_transaction:
        return 0
    found = 0
    for path in paths:
        tid = path.name
        runs = board_runs(hub, board, tid)
        if runs is None:
            continue
        with write_txn(conn):
            if (conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone()
                    or not _plain(path) or preserved(tid, path)):
                continue
            _ensure(conn)
            if conn.execute(f"SELECT 1 FROM {_TABLE} WHERE task_id=? OR workspace_path=?",
                            (tid, str(path))).fetchone():
                continue
            evidence = {"kind": "board", "board": board, "runs": runs}
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?, ?, ?)",
                         (tid, str(path), json.dumps(evidence, sort_keys=True)))
        found += 1
        log.info("Scratch custody discovered: task=%s path=%s kind=board runs=%d", tid, path, len(runs))
    return found
