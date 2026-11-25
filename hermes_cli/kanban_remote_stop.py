"""Archive a card another machine is running: a stop request through the Board, acted on by the lease holder. [daveey]

On a primary board a card this gateway created can run on another machine under that machine's Board lease
(worker nodes, ``kanban_worker_node``). The Board fences every status write on such a card to the lease holder
(``live_claim``), so archiving it here used to write ``archived`` locally, report success, and then have the push
rejected: the card read back ``running`` and the remote worker kept going.

Now the requester does not write the status at all. It appends an ``archive_requested`` event (events are not
fenced, so the Board accepts it from any gateway) and reports ``archive_pending``. The holder pulls the event into its
copy of the card, which wakes its dispatcher; that tick (:func:`apply_stop_requests`) archives the card there,
terminating its host-local worker, and the holder's status write (it owns the lease) reaches every gateway.

Every other lifecycle write (complete, block, schedule, unblock, review, reclaim, move, ...) on such a card is refused
before it writes, through :func:`remote_holder` with the one :func:`refusal` message: the CLI via
:func:`cli_refusal`, the kanban_* tools in ``tools/kanban_tools._remote_lease_refusal``. Before that, ``hermes kanban
complete`` printed ``Completed``, closed the run mirror (task_runs is not fenced, so that handoff stuck) and notified
subscribers, and only then did the Board reject the status (t_01a1ccfeab615947, 2026-11-13).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Optional

from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)

STOP_KIND = "archive_requested"
VIA = "remote_archive_request"


class RemoteArchiveRefused(ValueError):
    """The card runs under another machine's live lease and this board cannot carry a stop request to it."""

    code = "remote_live_lease"

    def __init__(self, task_id: str, holder: str, why: str) -> None:
        super().__init__(f"cannot archive {task_id}: it is running under {holder}'s live lease and {why}. "
                         f"Nothing changed. Archive it on {holder}, or wait for the run to end.")
        self.task_id, self.holder = task_id, holder
        self.tool_error_fields = {"holder": holder, "rejection": "live_claim"}


def remote_holder(conn: sqlite3.Connection, task_id: str, now_ms: Optional[int] = None) -> Optional[str]:
    """The other gateway whose live Board lease covers ``task_id`` (no local claim), else None.

    Only primary boards mirror leases. Timestamp expiry does not release a lease: the Board may retain it
    for a dark holder (up to 12h) until renewal, operator reset, or canonical expiry clears the row."""
    if sync.board_mode(conn) != "primary":
        return None
    row = conn.execute("SELECT status, claim_lock, lease_holder, lease_expires FROM tasks WHERE id = ?",
                       (task_id,)).fetchone()
    if row is None or not row["lease_holder"] or row["lease_expires"] is None or row["claim_lock"] is not None:
        return None
    own = str(sync._state(conn)["gateway_id"])
    # Keep now_ms as a compatibility argument; only a canonical clear releases remote custody.
    if row["lease_holder"] == own:
        return None
    return str(row["lease_holder"])


def refusal(verb: str, task_id: str, holder: str) -> str:
    """The one message every refused lifecycle write (CLI verb or kanban_* tool) prints under a remote lease."""
    return (f"cannot {verb} {task_id}: it is running under {holder}'s live lease. Nothing changed. "
            f"Let {holder}'s run finish, or archive it to stop it.")


# CLI verbs whose write moves a lifecycle column the Board fences to the lease holder (status, current_run_uid,
# result, started_at, completed_at). ``edit`` only with --result. ``archive`` is not here: it sends a stop request.
CLI_FENCED_VERBS = frozenset({
    "complete", "block", "schedule", "unblock", "request-review", "request-changes", "reopen-review", "reclaim",
    "move", "reassign", "promote", "reopen",
    # Not a status write, and the Board accepts it, but it changes who owns the holder's live run
    # (``assign_task`` refuses only a local claim_lock).
    "assign",
})


def cli_refusal(action: str, args: Any) -> Optional[str]:
    """For ``hermes kanban <action>``: the refusal for the first target card under another machine's live lease,
    else None. Checked before the handler runs, so a refused (bulk) command writes nothing; ``--force`` does not
    bypass it, because the Board rejects the write (``live_claim``) after the CLI would have printed success."""
    if action not in CLI_FENCED_VERBS and not (action == "edit" and getattr(args, "result", None) is not None):
        return None
    ids = list(getattr(args, "task_ids", None) or []) or [getattr(args, "task_id", None),
                                                          *(getattr(args, "ids", None) or [])]
    ids = [str(i) for i in ids if i]
    if not ids:
        return None
    try:
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            for tid in ids:
                holder = remote_holder(conn, tid)
                if holder is not None:
                    return refusal(action.replace("-", " "), tid, holder)
    except Exception:  # health: allow BLE001 -- the handler reports its own board errors
        _log.debug("kanban %s: remote lease check failed", action, exc_info=True)
    return None


def _run_uid(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                       (task_id,)).fetchone()
    return row[0] if row and row[0] else None


def request_archive(conn: sqlite3.Connection, task_id: str, holder: str, *, via: Optional[str] = None) -> dict:
    """Queue a stop request for ``holder`` (an ``archive_requested`` event pushed to the Board) and return
    ``{"status": "archive_pending", "holder": ...}``. Raises :class:`RemoteArchiveRefused` when the event would
    not reach the Board (nothing captured it into the outbox); nothing is left written in that case."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    own = str(sync._state(conn)["gateway_id"])
    payload: dict[str, Any] = {"holder": holder, "requested_by": own, "run_uid": _run_uid(conn, task_id)}
    if via:
        payload["via"] = via
    with write_txn(conn):
        before = conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_events'").fetchone()[0]
        kb._append_event(conn, task_id, STOP_KIND, payload, run_id=kb._current_run_id(conn, task_id))
        after = conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_events'").fetchone()[0]
        if after <= before:
            # The capture trigger did not record it: the Board would never hear of it. Undo and refuse.
            conn.execute("DELETE FROM task_events WHERE id = (SELECT MAX(id) FROM task_events WHERE task_id = ? "
                         "AND kind = ?)", (task_id, STOP_KIND))
            raise RemoteArchiveRefused(task_id, holder, "this board cannot send it a stop request")
    # The sync loop sees the pending outbox row within a tick (``wait_for_work``) and pushes it.
    _log.info("kanban: archive of %s requested from %s (lease holder %s)", task_id, own, holder)
    return {"status": "archive_pending", "holder": holder}


def archive(conn: sqlite3.Connection, task_id: str, *, via: Optional[str] = None, signal_fn=None) -> Optional[dict]:
    """Archive ``task_id``, or send a stop request when another machine holds its live lease.

    Returns ``{"status": "archived"}``, ``{"status": "archive_pending", "holder": h}``, or None when the card
    cannot be archived (unknown, already archived). Raises :class:`RemoteArchiveRefused`."""
    from hermes_cli import kanban_db as kb
    holder = remote_holder(conn, task_id)
    if holder is not None:
        return request_archive(conn, task_id, holder, via=via)
    return {"status": "archived"} if kb.archive_task(conn, task_id, via=via, signal_fn=signal_fn) else None


def _wants_stop(conn: sqlite3.Connection, task_id: str, run_uid: Optional[str], run_started: int) -> Optional[dict]:
    """The newest stop request for the card's current run, else None."""
    for kind_payload, created_at in conn.execute(
            "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC",
            (task_id, STOP_KIND)).fetchall():
        try:
            payload = json.loads(kind_payload or "{}")
        except ValueError:
            payload = {}
        asked_run = payload.get("run_uid")
        if asked_run:
            if asked_run == run_uid:
                return payload
            continue  # aimed at an earlier run of the card
        if int(created_at or 0) >= int(run_started or 0):
            return payload
    return None


def apply_stop_requests(conn: sqlite3.Connection, *, signal_fn: Optional[Callable] = None) -> list[str]:
    """Holder side (each dispatcher tick): archive every card this gateway runs under its own lease that has a
    stop request for the current run. ``archive_task`` terminates the host-local worker; the status write is
    the lease holder's, so the Board accepts it. Returns the ids archived."""
    if sync.board_mode(conn) != "primary":
        return []
    from hermes_cli import kanban_db as kb
    own = str(sync._state(conn)["gateway_id"])
    rows = conn.execute(
        "SELECT t.id, r.uid AS run_uid, COALESCE(r.started_at, t.started_at, 0) AS run_started "
        "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.claim_lock IS NOT NULL AND t.lease_holder = ? "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = ?)", (own, STOP_KIND)).fetchall()
    done: list[str] = []
    for row in rows:
        request = _wants_stop(conn, row["id"], row["run_uid"], row["run_started"])
        if request is None:
            continue
        try:
            if kb.archive_task(conn, row["id"], via=VIA, signal_fn=signal_fn):
                done.append(row["id"])
                _log.info("kanban: archived %s on a stop request from %s", row["id"], request.get("requested_by"))
        except Exception:  # health: allow BLE001 -- one card's archive must not stop the dispatcher tick
            _log.warning("kanban: stop request for %s failed", row["id"], exc_info=True)
    return done
