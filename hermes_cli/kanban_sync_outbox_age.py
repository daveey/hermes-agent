"""How long local edits wait in a synced board's outbox, and the alarms on it. [daveey]

Incident (2026-11-12, David-MBP, t_01a1d36ddcea59fe): 1747 local edits waited up to 9 h in the outbox, every one
with ``attempts=0``, because the sync round was stuck draining a pull backlog before its push. ``sync status``
showed only a count, nothing alerted, and the stall surfaced only as downstream "ready with no worker" notices.

Three readers of one number, the age of the oldest pending op:

- ``hermes kanban sync status`` prints it (``oldest_pending_age_s``; every pending row).
- The sync loop logs a WARNING (:func:`after_round`, at most once per :data:`STALE_S` per board) when a round
  succeeded and an unanswered op is still older than :data:`STALE_S`.
- The dispatcher tick (:func:`check`) files ONE card per incident on the default board when the same op is that old
  and this process's sync loop is not failing (its last round succeeded, or a round has run for over
  :data:`STALE_S`, which is the stuck-round shape of the incident). The incident ends when the op is answered; the
  next one comments on the open card (at most hourly) or opens a new card once that card is done.

"Unanswered" = pending with no ``retry_after``: the Board never told us to wait. A row the Board deferred
(``storage_error``, ``depends_on_rejected``) retries on its own backoff and logs its own reason; counting it would
alarm on a Board-side wait, not on a push that never runs.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

STALE_S = 600.0
CHECK_EVERY_S = 60.0
KEY = "kanban-sync-outbox-stalled"
AUTHOR = "kanban-sync-outbox"
_COMMENT_EVERY = 3600

_LOCK = threading.Lock()
_LAST_OK: dict[str, bool] = {}  # board slug -> this process's last completed round for it succeeded
_ROUND_STARTED: list[Optional[float]] = [None]  # monotonic start of the loop's round in progress
_WARNED_AT: dict[str, float] = {}
_FILED: set[str] = set()
_NEXT_CHECK: list[float] = [0.0]


def oldest_pending_age_s(conn: sqlite3.Connection, *, unanswered: bool = False, held_runs: bool = False,
                         now: Optional[float] = None) -> Optional[float]:
    """Seconds since the oldest pending outbox op was queued (None: no pending op). ``unanswered`` counts only ops
    the Board never deferred; ``held_runs`` leaves out run rows, which a round behind the feed head holds back on
    purpose (``push(hold_runs=...)``)."""
    sql = "SELECT MIN(created_at) FROM kanban_sync_outbox WHERE state = 'pending'"
    sql += " AND retry_after IS NULL" if unanswered else ""
    sql += " AND table_name != 'task_runs'" if held_runs else ""
    row = conn.execute(sql).fetchone()
    if not row or row[0] is None:
        return None
    return max(0.0, (time.time() if now is None else now) - float(row[0]) / 1000)  # created_at is in ms


def _age(path: Path) -> Optional[float]:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_sync_backlog as backlog
    if not Path(path).exists():
        return None  # never create a board file just to read its outbox
    try:
        with kbc.connect_closing(path) as conn:
            return oldest_pending_age_s(conn, unanswered=True, held_runs=backlog.behind(path))
    except (sqlite3.Error, OSError):
        return None


def round_started(mono: Callable[[], float] = time.monotonic) -> None:
    """The sync loop starts a round (the dispatcher can then tell a round stuck for an hour from an idle loop)."""
    with _LOCK:
        _ROUND_STARTED[0] = mono()


def after_round(results: Iterable[Any], boards: Iterable[tuple[str, Path]], *,
                mono: Callable[[], float] = time.monotonic, age: Callable[[Path], Optional[float]] = _age) -> list[str]:
    """Record each board's round outcome and WARN when a good round left an unanswered op older than
    :data:`STALE_S`. Returns the boards warned about. Never raises."""
    warned: list[str] = []
    try:
        paths = dict(boards)
        with _LOCK:
            _ROUND_STARTED[0] = None
            for r in results:
                _LAST_OK[r.board] = not r.error
        for r in results:
            if r.error or r.board not in paths:
                continue
            seconds = age(paths[r.board])
            if seconds is None or seconds < STALE_S:
                _WARNED_AT.pop(r.board, None)
                continue
            now = mono()
            if now - _WARNED_AT.get(r.board, now - STALE_S) < STALE_S:
                continue
            _WARNED_AT[r.board] = now
            warned.append(r.board)
            log.warning("kanban sync: %s: a local edit has waited %.0f s in the outbox although this round succeeded "
                        "(the Board has not seen it; `hermes kanban sync status` shows the outbox)", r.board, seconds)
    except Exception:  # health: allow BLE001 -- an alarm must never stop the sync loop
        log.warning("kanban sync: outbox age check failed", exc_info=True)
    return warned


def _loop_healthy(slug: str, now: float) -> bool:
    with _LOCK:
        started = _ROUND_STARTED[0]
        return _LAST_OK.get(slug) is True or (started is not None and now - started >= STALE_S)


def check(*, mono: Callable[[], float] = time.monotonic, file_fn: Optional[Callable[[str, str], Optional[str]]] = None,
          boards: Optional[list[tuple[str, Path]]] = None,
          age: Callable[[Path], Optional[float]] = _age) -> list[str]:
    """One dispatcher-tick observation (at most every :data:`CHECK_EVERY_S`). Returns the card ids it filed or
    commented. Never raises."""
    try:
        now = mono()
        if now < _NEXT_CHECK[0]:
            return []
        _NEXT_CHECK[0] = now + CHECK_EVERY_S
        if boards is None:
            from hermes_cli import kanban_db_sync as sync
            if not sync.sync_enabled():
                return []
            from hermes_cli import kanban_sync_client as ksc
            boards = ksc.board_paths()
        out = []
        for slug, path in boards:
            seconds = age(path) if _loop_healthy(slug, now) else None
            if seconds is None or seconds < STALE_S:
                _FILED.discard(slug)  # answered (or the loop is failing, which other alarms cover): incident over
                continue
            if slug in _FILED:
                continue
            _FILED.add(slug)
            tid = (file_fn or report)(slug, f"board `{slug}`: the oldest unanswered outbox op is {seconds / 60:.0f} "
                                            f"min old while this host's sync loop is not failing")
            if tid:
                out.append(tid)
        return out
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban: sync outbox age check failed", exc_info=True)
        return []


def report(slug: str, detail: str) -> Optional[str]:
    """Open or comment the stalled-outbox card on the default board. Best effort: never raises."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
            return _report(conn, slug, detail)
    except Exception:  # health: allow BLE001 -- runs on the dispatcher tick
        log.warning("kanban: could not file the stalled sync outbox card (%s)", detail, exc_info=True)
        return None


def _report(conn: sqlite3.Connection, slug: str, detail: str) -> str:
    from hermes_cli import kanban_db as kb
    log.warning("kanban sync: outbox stalled: %s", detail)
    key = f"{KEY}:{slug}"
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY status IN ('done', 'failed', 'cancelled'), created_at DESC, rowid DESC LIMIT 1", (key,),
    ).fetchone()
    if row and row[1] not in ("done", "failed", "cancelled"):
        recent = conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND created_at >= ? LIMIT 1",
                              (row[0], AUTHOR, int(time.time()) - _COMMENT_EVERY)).fetchone()
        if not recent:
            kb.add_comment(conn, row[0], AUTHOR, f"Again: {detail}")
        return row[0]
    body = (
        "A local edit on this host has waited over 10 minutes in the Kanban sync outbox, so the Board (and every "
        "worker leasing from it) has not seen it, although the sync loop is not failing.\n\n"
        f"{detail}\n\n"
        "Check: `hermes kanban sync status` (cursor, pending, oldest_pending_age_s) and agent.log `kanban sync:` "
        "lines. A round pushes before and after its pulls and reads at most 20 feed pages per database "
        "(`hermes_cli/kanban_sync_backlog.py`), so an old op means push is not reached or not accepted. Done when "
        "the op is answered and the cause is fixed (precedent: t_01a1d36ddcea59fe)."
    )
    tid = kb.create_task(conn, title=f"Kanban sync: {slug} outbox holds a local edit unsent for over 10 min",
                         body=body, assignee="default", created_by=AUTHOR, priority=50,
                         idempotency_key=None if row else key)
    if row:  # the previous card is finished: the key now names this one (newest wins)
        with kb.write_txn(conn, allow_nested=True):
            conn.execute("UPDATE tasks SET idempotency_key = ? WHERE id = ?", (key, tid))
    return tid


def reset() -> None:
    """Tests: forget every observation."""
    with _LOCK:
        _LAST_OK.clear()
        _ROUND_STARTED[0] = None
    _WARNED_AT.clear()
    _FILED.clear()
    _NEXT_CHECK[0] = 0.0
