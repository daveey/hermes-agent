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

Detection: when an awaited card reached done/archived and its park is still ``scheduled`` more than
``LAG_WARN_SECONDS`` later, the wake-check tick logs one ``awaited_card_done_park_lag`` WARNING (``report_lag``).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

LAG_WARN_SECONDS = 120
_MAX_WATCHED = 1000  # in-memory lag records; the oldest are dropped past this
_WAIT_CHECK_TASK = re.compile(r"\bwait-check\b[^;&|\n]*?\btask\s+(t_[0-9a-f]{8,16})\b")
_FINISHED = ("done", "archived")
_LOCK = threading.Lock()
# (board db path, waiting card) -> (awaited card, its status, when this gateway applied it)
_WATCH: dict[tuple[str, str], tuple[str, str, float]] = {}


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


def _expedite(live: sqlite3.Connection, changed: Mapping[str, str], now: float) -> list[str]:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_db_connect import write_txn

    try:
        rows = live.execute(
            "SELECT w.task_id, w.cmd FROM task_wake_checks w JOIN tasks t ON t.id = w.task_id "
            "WHERE t.status = 'scheduled' AND w.purpose IS NOT 'ask_resolved'").fetchall()
    except sqlite3.OperationalError:  # a board without the probe table (older schema): nothing parks on probes
        return []
    hits = []
    for task_id, cmd in rows:
        refs = sorted(named_cards(live, task_id, cmd) & set(changed))
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
    _remember(key, hits, changed, now)
    if moved:
        sync.wake_dispatcher()
    return moved


def _remember(key: str, hits: list, changed: Mapping[str, str], now: float) -> None:
    with _LOCK:
        for task_id, _cmd, ref in hits:
            if changed[ref] in _FINISHED:
                _WATCH.setdefault((key, task_id), (ref, changed[ref], now))
            else:
                _WATCH.pop((key, task_id), None)
        while len(_WATCH) > _MAX_WATCHED:
            _WATCH.pop(next(iter(_WATCH)))


def report_lag(conn: sqlite3.Connection, *, now: Optional[float] = None) -> list[str]:
    """Log one WARNING per park still ``scheduled`` ``LAG_WARN_SECONDS`` after its awaited card finished (seen by
    ``expedite``), then forget it; a park that left ``scheduled`` is forgotten silently. Returns the warned cards.
    Reads only the parks ``expedite`` recorded, so it costs nothing while no awaited card finishes."""
    key = _db_key(conn)
    now = time.time() if now is None else float(now)
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
            warned.append(task_id)
            logger.warning("awaited_card_done_park_lag task=%s awaited=%s lag_s=%d (awaited card %s, park still "
                           "scheduled: its wake_check does not see it)", task_id, ref, int(now - seen), status)
    return warned


def _reset_for_tests() -> None:
    with _LOCK:
        _WATCH.clear()
