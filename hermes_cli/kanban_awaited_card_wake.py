"""A park waiting on a card re-probes when that card's status change reaches this gateway's replica. [daveey]

Incident (2026-11-13, t_01a1d57d2d295afd): PR #2277's owner t_01a1d4de561d5236 parked on
``hermes kanban wait-check task t_01a1d5749bb06b1f`` (``awaiting`` kind=card, check_every 900). The review card
completed on worker-1 at 07:14 PST; the owner's next probe was due 07:24, so it sat ``scheduled`` holding the land
lane until an operator woke it at 07:17. A same-board wait is a parent link and promotes at once
(``refresh_remote_parents``); a wait on a card another gateway owns was polled only every ``check_every``.

Now each sync round hands the replica pull's status changes here (``Pulled.status_changed``). A scheduled park
whose wake_check is ``wait-check task <id>`` or whose ``awaiting`` is ``{kind: card, ref: <id>}`` for a changed
card gets its probe due now and the dispatcher is woken, so the probe runs on the next tick (seconds) instead of up
to ``check_every`` later. The probe stays the judge: an unmet change (``--from``, an ``--until`` that excludes it)
reads "not yet" and the probe goes back to its cadence, so each status change costs at most one probe per park.
Nothing here polls: no status change, no work (hub-load design t_01a1d563324d7e8e).

Every replica apply path reports its changes (review 5894353204 on #2370, t_01a1d5aec0e3a0c5): the feed pull, a
snapshot rebuild on Resync (:func:`statuses` before the file is dropped, :func:`rebuilt` diffs it against the
rebuilt replica, with the pages a pull applied before its Resync, :func:`changes_page`), and ``kanban sync
reconcile``'s standalone pull (``kanban_sync_backlog.replica_at_head``).

Detection: when an awaited card reached done/archived and its park is still ``scheduled`` more than
``LAG_WARN_SECONDS`` later, the wake-check tick logs one ``awaited_card_done_park_lag`` WARNING (``report_lag``)
per park episode and awaited card: a park that re-parks still naming a finished card is warned again.
It reads the awaited cards' status from the replica itself (at most once per ``_SCAN_SECONDS`` per board), so a
status that arrived by any path is seen, not only one ``expedite`` was handed.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

LAG_WARN_SECONDS = 120
_SCAN_SECONDS = 60  # report_lag's replica read, per board: a lag is first seen at most this late
_MAX_WATCHED = 1000  # in-memory lag records; the oldest are dropped past this
_WAIT_CHECK_TASK = re.compile(r"\bwait-check\b[^;&|\n]*?\btask\s+(t_[0-9a-f]{8,16})\b")
_FINISHED = ("done", "archived")
_LOCK = threading.Lock()
# (board db path, waiting card) -> (awaited card, its status, when this gateway applied it)
_WATCH: dict[tuple[str, str], tuple[str, str, float]] = {}
# (board db path, waiting card) -> (awaited card, park episode) it was warned for: one warning per park episode and
# awaited card. A later replica scan re-arms it when the park re-parked (new episode) or names another finished
# card, and drops it once the park no longer waits on a finished card.
_WARNED: dict[tuple[str, str], tuple[str, Optional[int]]] = {}
_SCANNED: dict[str, float] = {}  # board db path -> when report_lag last read the replica


def named_cards(conn: sqlite3.Connection, task_id: str, cmd: Optional[str]) -> set[str]:
    """The cards a park waits on: its ``wait-check task <id>`` probe and its ``awaiting`` card ref."""
    from hermes_cli.kanban_awaiting import _TASK_ID, _latest_park

    refs = set(_WAIT_CHECK_TASK.findall(cmd or ""))
    awaiting, _reason = _latest_park(conn, task_id)
    if isinstance(awaiting, dict) and awaiting.get("kind") == "card":
        ref = str(awaiting.get("ref") or "")
        if _TASK_ID.fullmatch(ref):
            refs.add(ref)
    refs.discard(task_id)
    return refs


def _db_key(conn: sqlite3.Connection) -> str:
    from hermes_cli import kanban_db_connect as kbc
    return str(kbc._main_db_file(conn) or "")


def expedite(live: sqlite3.Connection, changed: Mapping[str, str], *, now: Optional[float] = None) -> list[str]:
    """Make due now the wake probe of every scheduled park on ``live`` that names a card in ``changed``
    ({card id: new status}, this round's replica apply). Returns the waiting cards whose probe moved earlier and
    wakes the dispatcher for them. Never raises: a failure leaves the probe on its own cadence."""
    if not changed:
        return []
    try:
        return _expedite(live, changed, time.time() if now is None else float(now))
    except (sqlite3.Error, ValueError, TypeError):
        logger.warning("kanban awaited-card wake: could not re-check the parks naming %d changed card(s)",
                       len(changed), exc_info=True)
        return []


def _parks(conn: sqlite3.Connection) -> list[tuple[str, str, set[str]]]:
    """``(waiting card, probe cmd, cards it names)`` for every scheduled park's probe that names a card."""
    try:
        rows = conn.execute(
            "SELECT w.task_id, w.cmd FROM task_wake_checks w JOIN tasks t ON t.id = w.task_id "
            "WHERE t.status = 'scheduled' AND w.purpose IS NOT 'ask_resolved'").fetchall()
    except sqlite3.OperationalError:  # a board without the probe table (older schema): nothing parks on probes
        return []
    out = []
    for task_id, cmd in rows:
        refs = named_cards(conn, task_id, cmd)
        if refs:
            out.append((task_id, cmd, refs))
    return out


def _expedite(live: sqlite3.Connection, changed: Mapping[str, str], now: float) -> list[str]:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_db_connect import write_txn

    names, hits = set(changed), []
    for task_id, cmd, named in _parks(live):
        refs = sorted(named & names)
        if refs:
            hits.append((task_id, cmd, refs[0]))
    if not hits:
        return []
    key, due, moved = _db_key(live), int(now), []
    with write_txn(live):
        for task_id, cmd, ref in hits:
            cur = live.execute(
                "UPDATE task_wake_checks SET next_at = ? WHERE task_id = ? AND cmd = ? AND next_at > ? "
                "AND purpose IS NOT 'ask_resolved' AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND status = "
                "'scheduled')", (due, task_id, cmd, due, task_id))
            if cur.rowcount:
                moved.append(task_id)
                logger.info("kanban awaited-card wake: %s re-probes now: awaited card %s is %s", task_id, ref,
                            changed[ref])
    _remember(live, key, hits, changed, now)
    if moved:
        sync.wake_dispatcher()
    return moved


def _episode(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """The park episode: the id of the card's latest park event (the park ``named_cards`` reads its ref from)."""
    try:
        row = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued')",
                           (task_id,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _rearm(conn: sqlite3.Connection, key: str, task_id: str, ref: str) -> bool:
    """Whether the park may be watched for ``ref``: never warned, or warned for another awaited card or an earlier
    park episode (that mark is dropped). Call under ``_LOCK``."""
    mark = _WARNED.get((key, task_id))
    if mark is None:
        return True
    if mark == (ref, _episode(conn, task_id)):
        return False
    del _WARNED[(key, task_id)]
    return True


def _remember(live: sqlite3.Connection, key: str, hits: list, changed: Mapping[str, str], now: float) -> None:
    with _LOCK:
        for task_id, _cmd, ref in hits:
            if changed[ref] in _FINISHED:
                if _rearm(live, key, task_id, ref):
                    _WATCH.setdefault((key, task_id), (ref, changed[ref], now))
            else:
                _WATCH.pop((key, task_id), None)
        _bound()


def _bound() -> None:
    for table in (_WATCH, _WARNED):
        while len(table) > _MAX_WATCHED:
            table.pop(next(iter(table)))


def _replica_finished(conn: sqlite3.Connection) -> Optional[dict[str, tuple[str, str]]]:
    """``{waiting card: (awaited card, its replica status)}`` for each scheduled park naming a card the replica has
    done/archived; None when there is no readable replica (nothing to judge by)."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_replica_reader import open_replica

    with open_replica(kbc._main_db_file(conn)) as replica:
        if replica is None:
            return None
        parks = _parks(conn)
        refs = sorted({ref for _tid, _cmd, named in parks for ref in named})
        if not refs:
            return {}
        marks = ",".join("?" * len(refs))
        status = {r[0]: r[1] for r in replica.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({marks})", refs).fetchall()}
    out: dict[str, tuple[str, str]] = {}
    for task_id, _cmd, named in parks:
        done = sorted(ref for ref in named if status.get(ref) in _FINISHED)
        if done and task_id not in out:
            out[task_id] = (done[0], status[done[0]])
    return out


def _scan(conn: sqlite3.Connection, key: str, now: float) -> None:
    """Record the parks whose awaited card the replica shows finished, whatever path applied it (re-arming a warned
    park that re-parked or now names another finished card), and forget the warned mark of a park that no longer
    waits on a finished card. At most once per ``_SCAN_SECONDS`` per board."""
    with _LOCK:
        last = _SCANNED.get(key)
        if last is not None and 0 <= now - last < _SCAN_SECONDS:
            return
        _SCANNED[key] = now
    try:
        found = _replica_finished(conn)
    except (sqlite3.Error, OSError, ValueError):
        logger.debug("kanban awaited-card wake: replica scan failed", exc_info=True)
        return
    if found is None:
        return
    with _LOCK:
        for task_id, (ref, status) in found.items():
            if _rearm(conn, key, task_id, ref):
                _WATCH.setdefault((key, task_id), (ref, status, now))
        for k in [k for k in _WARNED if k[0] == key and k[1] not in found]:
            del _WARNED[k]
        _bound()


def report_lag(conn: sqlite3.Connection, *, now: Optional[float] = None) -> list[str]:
    """Log one WARNING per park still ``scheduled`` ``LAG_WARN_SECONDS`` after its awaited card finished (seen by
    ``expedite`` when it applied, or by the replica scan at most ``_SCAN_SECONDS`` later), then mark it warned for
    that awaited card and park episode: a later scan warns again only after the park re-parks (a new episode) or
    names another finished card, and drops the mark once it no longer waits on a finished card. A recorded park that
    left ``scheduled`` before its warning is forgotten silently. Returns the warned cards. Beyond the throttled
    replica read it reads only the recorded parks (and a warned park's latest park event)."""
    key = _db_key(conn)
    now = time.time() if now is None else float(now)
    _scan(conn, key, now)
    with _LOCK:
        mine = [(tid, v) for (k, tid), v in _WATCH.items() if k == key]
    warned = []
    for task_id, (ref, status, seen) in mine:
        try:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        except sqlite3.Error:
            return warned
        if row is not None and row[0] == "scheduled" and now - seen < LAG_WARN_SECONDS:
            continue
        with _LOCK:
            _WATCH.pop((key, task_id), None)
            if row is not None and row[0] == "scheduled":
                _WARNED[(key, task_id)] = (ref, _episode(conn, task_id))
                _bound()
        if row is not None and row[0] == "scheduled":
            warned.append(task_id)
            logger.warning("awaited_card_done_park_lag task=%s awaited=%s lag_s=%d (awaited card %s, park still "
                           "scheduled: its wake_check does not see it)", task_id, ref, int(now - seen), status)
    return warned


# --- a snapshot rebuild's status changes ------------------------------------------------------------------------

def changes_page(hub: Any, board: str, cursor: int, applied: Mapping[str, str]) -> dict:
    """``hub.changes(board, cursor)``; a Resync it raises carries ``status_changed``, the status changes the pull
    already applied from earlier pages (the rebuild's pre-read sees them as the old state, so its diff would not)."""
    from hermes_cli.kanban_sync_client import HubError, Resync
    try:
        page = hub.changes(board, cursor)
    except Resync as exc:
        exc.status_changed = dict(applied)  # type: ignore[attr-defined]
        raise
    if not isinstance(page, dict):
        raise HubError(200, 'changes envelope malformed')
    return page


def _statuses(conn: sqlite3.Connection) -> Optional[dict[str, str]]:
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, status FROM tasks").fetchall()}
    except sqlite3.Error:
        logger.warning("kanban awaited-card wake: replica statuses unreadable around a rebuild", exc_info=True)
        return None


def statuses(replica: sqlite3.Connection, applied: Optional[Mapping[str, str]] = None) -> tuple:
    """The replica's card statuses read before a rebuild drops it (None when unreadable: then every rebuilt card
    counts as changed), with the status changes the failed pull had already applied (``changes_page``)."""
    return _statuses(replica), dict(applied or {})


def rebuilt(before: tuple, replica: sqlite3.Connection) -> dict[str, str]:
    """``{card id: status}`` for every card whose status the rebuild changed (or brought in), plus those the failed
    pull had changed, at their rebuilt status: what ``expedite`` gets for the round. Never raises."""
    old, applied = before
    new = _statuses(replica)
    if new is None:
        return {}
    out = {tid: s for tid, s in new.items() if old is None or old.get(tid) != s}
    out.update({tid: new[tid] for tid in applied if tid in new})
    return out


def _reset_for_tests() -> None:
    with _LOCK:
        _WATCH.clear()
        _WARNED.clear()
        _SCANNED.clear()
