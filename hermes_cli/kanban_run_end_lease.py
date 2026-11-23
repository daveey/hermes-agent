"""Free the Board lease when the holder's own transition ends its run. [daveey]

On a primary board the Board frees a run's lease when the pushed card row leaves ``running`` (rows.ts:
the holder moving its card out of ``running`` ends the lease). With this host's push loop down, that
row never leaves the outbox, the lease outlives the run, and at expiry the Board's stale-lease reaper
books it as a crash (``reclaimed`` + ``failure_finalized``, ``stale_lease=<holder>``): a card that
parked normally trips its retry breaker (t_01a1d05e8be13a6a, David-MBP 2026-11-13, sync down on
SchemaAhead). Claims and renewals still reached the Board as direct calls; only the release was lost.

So every run-ending transition the holder makes (schedule, block, complete, review handoff, request
changes, continue, archive, descendant reopen) queues :func:`note` inside its write txn, and
``write_txn`` calls :func:`flush` after COMMIT: a direct ``release`` call, best-effort, the way
operator reclaim already does. A failed release keeps today's fallback (the lease expires on the
server) and never fails the transition. Crash, timeout and reclaim accounting paths do not queue: they
release with their own outcome, and a neutral release sent first would hide the charge. (Archive and
descendant reopen close the run ``reclaimed`` too, but as uncharged operator transitions, so they queue.)

Only while the outbox is stalled (:func:`outbox_stalled`) for the immediate
post-commit attempt. ``kanban_run_end_retry`` also captures a durable obligation
in the ending transaction: after the same 120s grace the dispatcher retries it
independently of the push loop, including when no card is still running. This
covers a fresh queue that stalls only AFTER the one-shot callback has returned.
Board ``releaseTask`` returns a card it still
sees ``running`` to ``ready`` (and drops a placement card's ``runs_on``). On a healthy host the direct
release would usually beat the row push, so a parked or done card would read ``ready`` on the Board
until the push lands, open to a claim from another machine. There the row push ends the lease itself,
so nothing is sent.

The stalled case has the same window, accepted: the direct release makes the Board return the card to
``ready`` at once (``retryStatusForRun``) and drop a placement card's ``runs_on``, so another machine may
claim a card that is done or parked here until this host's outbox drains. Without the release the same
window opened anyway, only later (at lease expiry) and through the stale-lease reaper, which also
charged the run as a crash.

``_PENDING`` is keyed by ``id(conn)``: ``sqlite3.Connection`` takes no weak references, so a
``WeakKeyDictionary`` is not an option. Only ``write_txn`` drains it (``flush`` on COMMIT or rollback),
and every caller of :func:`note` runs inside ``write_txn``; outside a transaction ``note`` releases at
once. Were an entry ever stranded (a raw ``BEGIN`` caller, a connection closed mid-txn) and its id reused
by a later connection, that connection's next commit would send at most the release of a run that
:func:`_release` finds ended in that connection's own ``task_runs`` (a run of another board is not found,
so nothing is sent), and only while its outbox is stalled.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger("hermes_cli.kanban_db_sync")

# A due pending outbox row older than this means the push loop is not draining (a healthy loop pushes
# within seconds; a held row with a future ``retry_after`` does not count).
OUTBOX_STALL_SECONDS = 120

# id(conn) -> [(task_id, run_uid)] queued inside the open write txn of that connection (keying and
# draining invariant: module docstring).
_PENDING: dict[int, list[tuple[str, str]]] = {}


def outbox_stalled(conn: sqlite3.Connection, now: Optional[float] = None) -> bool:
    """True when a due ``pending`` outbox row is older than :data:`OUTBOX_STALL_SECONDS`."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT MIN(created_at) FROM kanban_sync_outbox WHERE state = 'pending' "
        "AND (retry_after IS NULL OR retry_after <= ?)", (int(now),)).fetchone()
    return bool(row) and row[0] is not None and int(row[0]) <= int((now - OUTBOX_STALL_SECONDS) * 1000)


def note(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> None:
    """Queue the lease release for ``run_id``, which the holder's transition just ended in this txn."""
    from hermes_cli import kanban_db_sync as sync
    if run_id is None or sync.board_mode(conn) != "primary":
        return
    uid = sync.run_uid(conn, run_id)
    if uid is None:
        return
    from hermes_cli import kanban_run_end_retry
    if not conn.in_transaction:
        from hermes_cli.kanban_db_connect import write_txn
        with write_txn(conn):
            note(conn, task_id, run_id)
        return
    kanban_run_end_retry.note(conn, task_id, uid)
    _PENDING.setdefault(id(conn), []).append((task_id, uid))


def flush(conn: sqlite3.Connection, *, committed: bool) -> None:
    """``write_txn`` after COMMIT (``committed``) or rollback (drops the queue)."""
    pending = _PENDING.pop(id(conn), None) if _PENDING else None
    if not committed or not pending:
        return
    for task_id, uid in pending:
        _release(conn, task_id, uid)


def _release(conn: sqlite3.Connection, task_id: str, uid: str) -> None:
    from hermes_cli import kanban_db_sync as sync
    try:
        row = conn.execute("SELECT ended_at FROM task_runs WHERE uid = ?", (uid,)).fetchone()
        if row is None or row[0] is None:
            return  # the end did not commit (a rolled-back savepoint): the run still holds its lease
        if not outbox_stalled(conn):
            return  # the row push ends the lease on the Board, with no window where the card reads ready
        _log.info("kanban sync: outbox stalled; releasing the Board lease of %s run %s at its end", task_id, uid)
        from hermes_cli import kanban_run_end_retry
        kanban_run_end_retry.send(conn, task_id, uid)
    except Exception as exc:  # health: allow BLE001 -- best-effort: the lease expires on the server instead
        _log.warning("kanban sync: lease release for %s run %s failed (%s: %s); it expires on the server",
                     task_id, uid, type(exc).__name__, exc)
