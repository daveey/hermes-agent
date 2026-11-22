"""Durable, exact-run updater reclaim commands, committed before termination.

The intent is the linearization point of an authorized pause, not an inference
from EOF/SIGTERM. Observers finish an abandoned command (including process custody)
instead of turning its termination into a failure. No database lock spans polling.
This local table is deliberately not replicated as Board authority.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from uuid import uuid4

from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON

TABLE = "kanban_update_reclaim_intents"
FIELDS = ("current_run_id", "claim_lock", "worker_pid", "worker_started_at")
# Validated explicit-operator reclaim attribution; NULL on legacy and plain updater pauses.
ATTRIBUTION = ("urgency_source", "interrupt_target")


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() is not None


def _matching(intent, row):
    return intent is not None and row is not None and all(intent[key] == row[key] for key in FIELDS)


def _owner_alive(intent):
    from hermes_cli import kanban_db_dispatch as dispatch
    return dispatch._worker_alive(intent["owner_pid"], intent["owner_started_at"])


def observer_held(conn, row) -> bool:
    """Keep charged releases and updater-owned reclaims out of the crash sweep."""
    from hermes_cli.kanban_failure_release import observer_held as command_held
    return command_held(conn, row["current_run_id"]) or pending(conn, row)


def pending(conn, row) -> bool:
    """An abandoned command still owns its exact run until recovered, never expires."""
    if not _exists(conn):
        return False
    return _matching(conn.execute(f"SELECT * FROM {TABLE} WHERE task_id=?", (row["id"],)).fetchone(), row)


def task_pending(conn, task_id) -> bool:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row is not None and pending(conn, row)


def recover_and_drain(conn):
    """Before the observer's transaction, resume abandoned pauses then drain neutral results."""
    from hermes_cli.kanban_neutral_release import drain

    if _exists(conn):
        for intent in conn.execute(f"SELECT * FROM {TABLE}").fetchall():
            if _owner_alive(intent):
                continue
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (intent["task_id"],)).fetchone()
            if _matching(intent, row) and row["status"] == "running":
                _resume(conn, intent)
            else:
                _settle(conn, intent["task_id"], intent, intent["token"])
    return drain(conn)


def _adopt(conn, intent) -> bool:
    """Atomically take over this exact abandoned command, or do nothing.

    The snapshot read in recover_and_drain is only a filter: another observer may
    finish the same pause and the dispatcher start a replacement before we act.
    Ownership moves only while the same token, the same abandoned owner and the
    same attempt still hold; otherwise recovery is a no-op.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    owner_pid = os.getpid()
    owner_started_at = dispatch._process_fingerprint(owner_pid)
    if owner_started_at is None:
        return False
    with kb.write_txn(conn):
        current = conn.execute(f"SELECT * FROM {TABLE} WHERE task_id=? AND token=?",
                               (intent["task_id"], intent["token"])).fetchone()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (intent["task_id"],)).fetchone()
        if (current is None or (current["owner_pid"], current["owner_started_at"])
                != (intent["owner_pid"], intent["owner_started_at"])
                or not _matching(current, row) or row["status"] != "running"):
            return False
        conn.execute(f"UPDATE {TABLE} SET owner_pid=?, owner_started_at=? WHERE task_id=? AND token=?",
                     (owner_pid, owner_started_at, intent["task_id"], intent["token"]))
    return True


def _resume(conn, intent) -> bool:
    """Finish an adopted pause against its own attempt, never the task's current one."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_manual_reclaim import _reclaim

    task_id = intent["task_id"]
    if kb._sync.refuse_foreign_reclaim(conn, task_id) or not _adopt(conn, intent):
        return False
    attribution = kb._cu_requeue.reclaim_attribution(
        CORE_UPDATE_REQUEUE_REASON, *(intent[key] for key in ATTRIBUTION))
    try:
        # Termination targets the intent's PID + fingerprint, and the close is a CAS
        # on its run/claim/PID identity, so a replacement attempt is never touched.
        return _reclaim(conn, task_id, intent, CORE_UPDATE_REQUEUE_REASON, None, attribution)
    finally:
        _settle(conn, task_id, intent, intent["token"])


def _settle(conn, task_id, selected, token):
    """Drop a finished or stale command; keep an unfinished one for the next observer."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not _matching(selected, row) or row["status"] != "running":
            conn.execute(f"DELETE FROM {TABLE} WHERE task_id=? AND token=?", (task_id, token))
        else:
            # This call unwound without closing its run. Relinquish execution,
            # not the durable pause decision, so the next observer resumes it.
            conn.execute(f"UPDATE {TABLE} SET owner_pid=0 WHERE task_id=? AND token=?", (task_id, token))


@contextmanager
def fence(conn, task_id, selected, reason, attribution=None):
    """Publish before signalling; stale selection or concurrent reclaim sends no signal."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    if reason != CORE_UPDATE_REQUEUE_REASON:
        yield True
        return
    token = uuid4().hex
    owner_pid = os.getpid()
    owner_started_at = dispatch._process_fingerprint(owner_pid)
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        valid = (owner_started_at is not None and row is not None and row["status"] == "running"
                 and _matching(selected, row))
        if valid:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (task_id TEXT PRIMARY KEY "
                         "REFERENCES tasks(id) ON DELETE CASCADE, current_run_id INTEGER, claim_lock TEXT, "
                         "worker_pid INTEGER, worker_started_at TEXT, token TEXT NOT NULL, "
                         "owner_pid INTEGER NOT NULL, owner_started_at TEXT NOT NULL, "
                         "urgency_source TEXT, interrupt_target TEXT)")
            previous = conn.execute(f"SELECT * FROM {TABLE} WHERE task_id=?", (task_id,)).fetchone()
            valid = not (_matching(previous, row) and _owner_alive(previous))
        if valid:
            conn.execute(f"INSERT OR REPLACE INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (task_id, *(row[key] for key in FIELDS), token, owner_pid, owner_started_at,
                          *((attribution or {}).get(key) for key in ATTRIBUTION)))
    if not valid:
        yield False
        return
    try:
        yield True
    finally:
        _settle(conn, task_id, selected, token)
