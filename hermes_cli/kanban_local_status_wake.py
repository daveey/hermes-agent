"""A park waiting on a card re-probes when that card's status changes on this same gateway. [daveey]

Incident (2026-11-13, t_01a1d5def756990d): PR #2351's owner t_01a1d4bde1a32381 parked on worker-5 with
``hermes kanban wait-check task t_01a1d5c2abb6dcee`` (``awaiting`` kind=card, check_every 900). The review card was
created on worker-5, claimed there, and completed there at 16:43:01; the owner woke on its regular probe at 16:50:05.
PR #2370 (``kanban_awaited_card_wake.expedite``) re-probes such a park only when the card's status change arrives
by a replica pull; a change written on this gateway's own board never reached it.

Now every ``write_txn`` records the status changes it commits, and after COMMIT hands them to the same
``expedite``: a scheduled park whose ``wait-check task <id>`` probe or ``awaiting`` card ref names a changed card
gets its probe due now (next_at earlier only, re-checked inside expedite's own write txn) and the dispatcher is
woken (in the gateway process; another process's park is picked up on the dispatcher's next tick). The probe stays
the judge, so an unmet change costs one probe and the park goes back to its cadence. Nothing polls.

The recorder is a TEMP trigger (as in ``kanban_sync_own_writes``): it fires only for statements run on the
connection that created it, and its rows live in that connection's temp schema, so they commit and roll back with
the write that made them (a rolled-back transition never expedites). ``arm`` clears them at each outer BEGIN, so a
commit hands over only its own changes. Before calling ``expedite`` (which reads every scheduled park's latest park
event), one indexed query asks whether any scheduled park mentions a changed card at all; the common commit costs
that one read. Every failure is swallowed: the park keeps its ``check_every`` cadence and the transition stands.
"""
from __future__ import annotations

import logging
import sqlite3
import threading

logger = logging.getLogger(__name__)

_TABLE = "kanban_local_status_changed"
_TRIGGER = "kanban_local_status_changed_trg"
_PREFILTER_MAX = 32  # more changed cards than this in one commit (a sweep): let expedite read the parks once
_active = threading.local()

_MENTIONS = (
    "SELECT 1 FROM task_wake_checks w JOIN tasks t ON t.id = w.task_id "
    "WHERE t.status = 'scheduled' AND w.purpose IS NOT 'ask_resolved' AND w.task_id != ?1 "
    "AND (instr(w.cmd, ?1) > 0 OR EXISTS (SELECT 1 FROM task_events e WHERE e.kind IN ('scheduled', 'continued') "
    "AND e.task_id = w.task_id AND instr(e.payload, ?1) > 0)) LIMIT 1")


def arm(conn: sqlite3.Connection) -> None:
    """``write_txn`` right after its outer BEGIN: install the recorder on ``conn`` once, forget older rows."""
    try:
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_TABLE} (task_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {_TRIGGER} AFTER UPDATE OF status ON main.tasks "
                     f"WHEN OLD.status IS NOT NEW.status BEGIN "
                     f"INSERT OR REPLACE INTO {_TABLE} (task_id, status) VALUES (NEW.id, NEW.status); END")
        conn.execute(f"DELETE FROM temp.{_TABLE}")
    except sqlite3.Error:  # a board without ``tasks`` yet: nothing to record
        pass


def flush(conn: sqlite3.Connection) -> list[str]:
    """``write_txn`` after COMMIT: expedite the parks naming a card whose status this commit changed.
    Returns the waiting cards whose probe moved earlier. Never raises."""
    if getattr(_active, "busy", False):  # expedite's own write txn commits here too
        return []
    try:
        changed = dict(conn.execute(f"SELECT task_id, status FROM temp.{_TABLE}").fetchall())
    except sqlite3.Error:  # recorder not installed on this connection
        return []
    if not changed:
        return []
    _active.busy = True
    try:
        if not _mentioned(conn, changed):
            return []
        from hermes_cli.kanban_awaited_card_wake import expedite
        return expedite(conn, changed)
    except Exception:  # health: allow BLE001 -- best-effort: the park keeps its check_every cadence
        logger.warning("kanban local status wake: could not expedite parks for %d changed card(s)", len(changed),
                       exc_info=True)
        return []
    finally:
        _active.busy = False


def _mentioned(conn: sqlite3.Connection, changed: dict) -> bool:
    if len(changed) > _PREFILTER_MAX:
        return True
    try:
        return any(conn.execute(_MENTIONS, (tid,)).fetchone() for tid in changed)
    except sqlite3.OperationalError:  # a board without the probe table: nothing parks on probes
        return False
