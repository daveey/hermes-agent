"""How long local edits wait in a synced board's outbox, and the alarms on it. [daveey]

Incident (2026-11-12, David-MBP, t_01a1d36ddcea59fe): 1747 local edits waited up to 9 h in the outbox, every one
with ``attempts=0``, because the sync round was stuck draining a pull backlog before its push. ``sync status``
showed only a count, nothing alerted, and the stall surfaced only as downstream "ready with no worker" notices.

Wall age remains diagnostic; alarms require a continuously observed sendable identity:

- ``hermes kanban sync status`` prints it (``oldest_pending_age_s``; every pending row).
- The sync loop logs a WARNING (:func:`after_round`, at most once per :data:`STALE_S` per board) when a round
  succeeded and the same unanswered op has been observed for :data:`STALE_S` awake seconds.
- The dispatcher tick (:func:`check`) files ONE card per incident on the default board when that same op is observed
  for :data:`STALE_S` awake seconds
  and this process's sync loop is not failing (its last round succeeded, or a round has run for over
  :data:`STALE_S`, which is the stuck-round shape of the incident). The incident ends when the op is answered; the
  next one comments on the open card (at most hourly) or opens a new card once that card is done. A failing, stalled
  or stopped loop is the replica-freshness check's (``kanban_sync_replica_age``), which the same tick runs next.

Observations persist in each board's ``.outbox-age.json`` sidecar. A same-boot restart resumes them;
another boot, unreadable/no sendable op, identity change, or >600 awake seconds without observation resets them.
Both entry paths share the observation and report wall age, observed-awake age and op_id, never payloads.
Both sample the shared cron host_fully_awake probe once: a False sample excludes the interval since
the previous observation and suppresses warnings/cards. DarkWake advances monotonic time but is not
fully awake. This also excludes an idle lid-closed Mac on AC with an external display and an idle
lid-open Mac on battery, until someone is active. Non-macOS and probe failures read awake.

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

from hermes_cli import kanban_sync_replica_age as replica_age  # the same tick's replica-freshness alarm
from hermes_cli import kanban_sync_outbox_observation as observation

log = logging.getLogger(__name__)

STALE_S = 600.0
CHECK_EVERY_S = 60.0
KEY = "kanban-sync-outbox-stalled"
AUTHOR = "kanban-sync-outbox"
_COMMENT_EVERY = 3600

_LOCK = threading.Lock()
_LAST_OK: dict[str, bool] = {}  # board slug -> this process's last completed round for it succeeded
_ROUND_STARTED: list[Optional[float]] = [None]  # monotonic start of the loop's round in progress

_NEXT_CHECK: list[float] = [0.0]


def oldest_pending_age_s(conn: sqlite3.Connection, *, unanswered: bool = False,
                         now: Optional[float] = None) -> Optional[float]:
    """Seconds since the oldest pending outbox op was queued (None: no pending op). ``unanswered`` counts only ops
    the Board never deferred."""
    sql = "SELECT MIN(created_at) FROM kanban_sync_outbox WHERE state = 'pending'"
    sql += " AND retry_after IS NULL" if unanswered else ""
    row = conn.execute(sql).fetchone()
    return None if not row or row[0] is None else _since(row[0], now)


def _since(created_ms: Any, now: Optional[float] = None) -> float:
    return max(0.0, (time.time() if now is None else now) - float(created_ms) / 1000)  # created_at is in ms


class _NoBoardReads:
    """The alarm never calls the Board: a card the round did not read counts as sendable (``row_evidence`` -> {})."""

    def row_evidence(self, *_a: Any) -> dict:
        return {}


def sendable_age_s(conn: sqlite3.Connection, path: Path, now: Optional[float] = None) -> Optional[float]:
    """While a board is behind the feed head (``kanban_sync_backlog.behind``: its last completed round ended behind,
    or no round of this process has reached the head yet): the age of the oldest unanswered op among exactly the
    ops the push before the head would send (``kanban_sync_backlog.sendable_before_head``, with the latest round's
    Board reads).
    Held ops (from the first pending run row on, a stale wake on a card the Board completed) wait on purpose for the
    head, possibly over a long catch-up: they age the alarm again once the board reaches it. (t_01a1d41c2612ebc0)"""
    from hermes_cli import kanban_sync_backlog as backlog
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE state = 'pending' AND (retry_after IS NULL OR retry_after <= ?) "
        "ORDER BY created_at, rowid", (int(time.time()),))]  # push's own selection and order
    sendable, _ = backlog.sendable_before_head(conn, _NoBoardReads(), "", rows, backlog.evidence(path))
    ages = [r["created_at"] for r in sendable if r["retry_after"] is None]
    return _since(min(ages), now) if ages else None


def _sample_full(path: Path) -> Optional[tuple[str, float]]:
    """Original clock, backlog and sendability read for one board that is not structurally empty."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_sync_backlog as backlog
    from hermes_cli import kanban_sync_cpu as sync_cpu
    with sync_cpu.marked_cm(kbc.connect_closing(path), "full.acquire", "full.close") as conn:
        return sync_cpu.mark("full.query", _full_query, conn, path, backlog)


def _full_query(conn, path, backlog):
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE state = 'pending' "
        "AND (retry_after IS NULL OR retry_after <= ?) ORDER BY created_at, rowid",
        (int(time.time()),))]
    if backlog.behind(path):
        rows, _ = backlog.sendable_before_head(
            conn, _NoBoardReads(), "", rows, backlog.evidence(path))
    row = next((r for r in rows if r["retry_after"] is None), None)
    return (row["op_id"], _since(row["created_at"])) if row else None


def _sample(path: Path) -> Optional[tuple[str, float]]:
    """Oldest sendable unanswered identity and wall age; unknown reads reset observation."""
    from hermes_cli import kanban_sync_cpu as sync_cpu
    if not Path(path).exists():
        return None
    try:
        # Only an empty structural answer is reusable. A positive answer still
        # follows the original clock/backlog/sendability path below. The answer is
        # always an unanswered row (``retry_after IS NULL``) out of a subset of the
        # pending rows, so with none of those it is None whatever the clock or
        # backlog say: a board whose pending ops are all Board-deferred skips the
        # fresh schema open each round (Mac, t_01a203e5bc71cc65: 4.4 ms a round).
        from hermes_cli.kanban_sync_client import _WATCH

        from hermes_cli import kanban_sync_probe_diag as probe_diag
        empty = sync_cpu.detail("sample_query", lambda: _WATCH.query(
            path, "SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' "
            "AND retry_after IS NULL LIMIT 1", unchanged_ok=True))
        probe_diag.note(path, empty)
        if empty is not None and empty[1] is None:
            return sync_cpu.detail("sample_skip", lambda: None)
        return sync_cpu.detail("sample_full", _sample_full, path)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


def _age(path: Path) -> Optional[float]:
    sample = _sample(path)
    return sample[1] if sample else None


def _observed(path: Path, mono, age, awake=True):
    from hermes_cli import kanban_sync_cpu as sync_cpu
    sample = _sample(path)
    # Retain the age injection seam for callers suppressing outbox observations.
    if age is not _age and age(path) is None:
        sample = None
    now = mono()  # sample and timestamp under the same observation lock
    op_id = sample[0] if sample else None
    seen = sync_cpu.detail(
        "sample_observe",
        lambda op_id, now, awake: observation.observe(path, op_id, now, awake=awake),
        op_id,
        now,
        awake,
    )
    return seen, sample[1] if sample else None, now


def round_started(mono: Callable[[], float] = time.monotonic) -> None:
    """The sync loop starts a round (the dispatcher can then tell a round stuck for an hour from an idle loop)."""
    with _LOCK:
        _ROUND_STARTED[0] = mono()


def after_round(results: Iterable[Any], boards: Iterable[tuple[str, Path]], *,
                mono: Callable[[], float] = time.monotonic, age: Callable[[Path], Optional[float]] = _age,
                awake_fn: Optional[Callable[[], bool]] = None) -> list[str]:
    """Record round outcome and WARN after :data:`STALE_S` observed-awake seconds for one sendable op.
    Returns the boards warned about. Never raises.

    ``sync.detail/after_round.awake`` and ``.sample`` (and sample's query, skip or full,
    and observe) are inclusive diagnostics of this step's parts
    (``kanban_sync_cpu.detail``, ``outer=False``): never sum them with steps."""
    from hermes_cli import kanban_sync_cpu as sync_cpu
    awake = sync_cpu.detail("awake", replica_age.fully_awake, awake_fn)
    warned: list[str] = []
    try:
        paths = dict(boards)
        results = list(results)
        with _LOCK:
            _ROUND_STARTED[0] = None
            for r in results:
                _LAST_OK[r.board] = not r.error
        replica_age.note_round(results, mono=mono)  # the replica-freshness card names the last round's outcome
        for r in results:
            if r.board not in paths:
                continue
            path = paths[r.board]
            with observation.LOCK:
                if r.error:
                    observation.observe(path, None, mono())
                    continue
                seen, seconds, now = sync_cpu.detail("sample", _observed, path, mono, age, awake)
                if not awake or seen is None or now - seen.since < STALE_S:
                    continue
                if seen.warned_at is not None and now - seen.warned_at < STALE_S:
                    continue
                seen.warned_at = now
                observation.remember(path, seen)
                warned.append(r.board)
                log.warning("kanban sync: %s: op %s has waited %.0f wall s / %.0f observed-awake s in the "
                            "outbox, excluded %.0f s not fully awake, although this round succeeded "
                            "(`hermes kanban sync status` shows the outbox)",
                            r.board, seen.op_id, seconds, now - seen.since, seen.excluded)
    except Exception:  # health: allow BLE001 -- an alarm must never stop the sync loop
        log.warning("kanban sync: outbox age check failed", exc_info=True)
    return warned


def _loop_healthy(slug: str, now: float) -> bool:
    with _LOCK:
        started = _ROUND_STARTED[0]
        return _LAST_OK.get(slug) is True or (started is not None and now - started >= STALE_S)


def _dead_watcher() -> list[str]:
    """A crashed sync loop files through the hub courier; the 60 s outbox throttle must not delay it."""
    try:
        from hermes_cli import kanban_sync_watcher_alert as watcher_alert
        tid = watcher_alert.check()
        return [tid] if tid else []
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban: sync watcher death check failed", exc_info=True)
        return []


def check(*, mono: Callable[[], float] = time.monotonic, file_fn: Optional[Callable[[str, str], Optional[str]]] = None,
          boards: Optional[list[tuple[str, Path]]] = None,
          age: Callable[[Path], Optional[float]] = _age,
          awake_fn: Optional[Callable[[], bool]] = None) -> list[str]:
    """One dispatcher-tick observation (at most every :data:`CHECK_EVERY_S`): the outbox, then each replica's
    freshness (``kanban_sync_replica_age.check``). Returns the card ids filed or commented. Never raises."""
    filed = _dead_watcher()
    try:
        now = mono()
        if now < _NEXT_CHECK[0]:
            return filed
        _NEXT_CHECK[0] = now + CHECK_EVERY_S
        if boards is None:
            from hermes_cli import kanban_db_sync as sync
            if not sync.sync_enabled():
                return filed
            from hermes_cli import kanban_sync_client as ksc
            boards = ksc.board_paths()
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban: sync outbox age check failed", exc_info=True)
        return filed
    awake = replica_age.fully_awake(awake_fn)
    return filed + _check_outbox(boards, mono, file_fn, age, awake) + replica_age.check(
        boards, now=now, awake_fn=lambda: awake)


def _check_outbox(boards: list[tuple[str, Path]], mono, file_fn, age, awake=True) -> list[str]:
    try:
        out = []
        for slug, path in boards:
            with observation.LOCK:
                # Observe even before loop health is established: a stuck first round
                # must not acquire an extra ten-minute delay. Replica-age covers failure.
                seen, seconds, now = _observed(path, mono, age, awake)
                if not awake or not _loop_healthy(slug, now) or seen is None or now - seen.since < STALE_S or seen.filed:
                    continue
                detail = (f"board `{slug}`: oldest sendable unanswered op `{seen.op_id}`; "
                          f"wall age {seconds / 60:.0f} min; observed-awake age {now - seen.since:.0f} s; "
                          f"excluded {seen.excluded:.0f} s not fully awake "
                          "while this host's sync loop is not failing")
                tid = (file_fn or report)(slug, detail)
                if tid:
                    seen.filed = True
                    observation.remember(path, seen)
                    out.append(tid)
        return out
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban: sync outbox age check failed", exc_info=True)
        return []


def report(slug: str, detail: str, *, key: str = KEY, author: str = AUTHOR, title: Optional[str] = None,
           body: Optional[str] = None, what: str = "outbox stalled", comment: Optional[str] = None) -> Optional[str]:
    """Open or comment the stalled-outbox card on the default board (or, with ``key``/``author``/``title``/
    ``body``, another alarm's card on the same one-card-per-incident path). ``comment``: posted verbatim on an open
    card and never throttled (news the card must carry, e.g. a board joining a host incident). Never raises."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        log.warning("kanban sync: %s: %s", what, detail)
        with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
            return _report(conn, slug, detail, key=key, author=author, title=title, body=body, comment=comment)
    except Exception:  # health: allow BLE001 -- runs on the dispatcher tick
        log.warning("kanban: could not file the %s card (%s)", what, detail, exc_info=True)
        return None


def _report(conn: sqlite3.Connection, slug: str, detail: str, *, key: str = KEY, author: str = AUTHOR,
            title: Optional[str] = None, body: Optional[str] = None, comment: Optional[str] = None) -> str:
    from hermes_cli import kanban_db as kb
    key = f"{key}:{slug}"
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY status IN ('done', 'failed', 'cancelled'), created_at DESC, rowid DESC LIMIT 1", (key,),
    ).fetchone()
    if row and row[1] not in ("done", "failed", "cancelled"):
        if comment is not None:
            kb.add_comment(conn, row[0], author, comment)
            return row[0]
        recent = conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND created_at >= ? LIMIT 1",
                              (row[0], author, int(time.time()) - _COMMENT_EVERY)).fetchone()
        if not recent:
            kb.add_comment(conn, row[0], author, f"Again: {detail}")
        return row[0]
    body = body or (
        "The same sendable local edit on this host has been observed for 10 awake minutes in the Kanban sync "
        "outbox, so the Board (and every "
        "worker leasing from it) has not seen it, although the sync loop is not failing.\n\n"
        f"{detail}\n\n"
        "Check: `hermes kanban sync status` (cursor, pending, oldest_pending_age_s) and agent.log `kanban sync:` "
        "lines. A round pushes before and after its pulls and reads at most 20 feed pages per database "
        "(`hermes_cli/kanban_sync_backlog.py`), so an old op means push is not reached or not accepted. Done when "
        "the op is answered and the cause is fixed (precedent: t_01a1d36ddcea59fe)."
    )
    tid = kb.create_task(conn, title=title or f"Kanban sync: {slug} outbox holds a local edit unsent for 10 awake min",
                         body=body, assignee="default", created_by=author, priority=50,
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

    _NEXT_CHECK[0] = 0.0
    replica_age.reset()
    observation.reset()
