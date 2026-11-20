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
# [daveey] Comments too (t_01a1f5d00bc5fa08, 2026-11-20): a worker parked on ``hermes kanban wait-comment <card>``
# for its orchestrator's answer (check_every 900). The answer was synced in 6 s and 2.5 min after each park, yet
# the probe slept to its next 15-minute slot; the owner-wake sweep reported the card "waiting on chat" meanwhile and
# the chat released it by hand twice. Each comment insert is recorded the same way and makes due now the
# wait-comment probe of a scheduled park on that card; the probe stays the judge.
_COMMENT_TABLE = "kanban_local_comment_added"
_COMMENT_TRIGGER = "kanban_local_comment_added_trg"
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
    try:
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_COMMENT_TABLE} (task_id TEXT PRIMARY KEY)")
        conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {_COMMENT_TRIGGER} AFTER INSERT ON main.task_comments "
                     f"BEGIN INSERT OR IGNORE INTO {_COMMENT_TABLE} (task_id) VALUES (NEW.task_id); END")
        conn.execute(f"DELETE FROM temp.{_COMMENT_TABLE}")
    except sqlite3.Error:  # a board without ``task_comments`` yet
        pass


def flush(conn: sqlite3.Connection) -> list[str]:
    """``write_txn`` after COMMIT: expedite the parks naming a card whose status this commit changed, and the
    wait-comment park of a card this commit commented on. Returns the waiting cards whose probe moved earlier.
    Never raises."""
    if getattr(_active, "busy", False):  # expedite's own write txn commits here too
        return []
    # Read both recorders before any write: an expedite's own write_txn re-arms (clears) them.
    try:
        changed = dict(conn.execute(f"SELECT task_id, status FROM temp.{_TABLE}").fetchall())
    except Exception:  # health: allow BLE001 -- no recorder here, or a non-sqlite stand-in: never raise into the commit
        changed = {}
    try:
        commented = [r[0] for r in conn.execute(f"SELECT task_id FROM temp.{_COMMENT_TABLE}").fetchall()]
    except Exception:  # health: allow BLE001 -- no recorder here: never raise into the commit
        commented = []
    if not changed and not commented:
        return []
    _active.busy = True
    try:
        moved = _flush_comments(conn, commented) if commented else []
        if not changed or not _mentioned(conn, changed):
            return moved
        from hermes_cli.kanban_awaited_card_wake import expedite
        return moved + expedite(conn, changed)
    except Exception:  # health: allow BLE001 -- best-effort: the park keeps its check_every cadence
        logger.warning("kanban local status wake: could not expedite parks for %d changed card(s)", len(changed),
                       exc_info=True)
        return []
    finally:
        _active.busy = False


_COMMENT_WHERE = ("task_id = ?2 AND next_at > ?1 AND purpose IS NOT 'ask_resolved' "
                  "AND instr(cmd, 'wait-comment ' || ?2) > 0 "
                  "AND EXISTS (SELECT 1 FROM tasks WHERE id = ?2 AND status = 'scheduled')")
_COMMENT_PARK = "SELECT 1 FROM task_wake_checks WHERE " + _COMMENT_WHERE
_COMMENT_DUE = "UPDATE task_wake_checks SET next_at = ?1 WHERE " + _COMMENT_WHERE


def _flush_comments(conn: sqlite3.Connection, commented: list[str]) -> list[str]:
    """A comment this commit added on a card parked on ``wait-comment <that card>``: its probe is due now."""
    import time

    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_db_connect import write_txn

    due = int(time.time())
    try:
        hits = [t for t in commented if conn.execute(_COMMENT_PARK, (due, t)).fetchone()]
    except sqlite3.OperationalError:  # a board without the probe table: nothing parks on probes
        return []
    if not hits:  # the common comment: one indexed read, no write
        return []
    moved: list[str] = []
    try:
        with write_txn(conn):
            for task_id in hits:
                if conn.execute(_COMMENT_DUE, (due, task_id)).rowcount:
                    moved.append(task_id)
                    logger.info("kanban comment wake: %s re-probes now: a comment arrived on its wait-comment park",
                                task_id)
        if moved:
            sync.wake_dispatcher()
    except Exception:  # health: allow BLE001 -- best-effort: the park keeps its check_every cadence
        logger.warning("kanban comment wake: could not expedite %d commented card(s)", len(hits), exc_info=True)
    return moved


def _mentioned(conn: sqlite3.Connection, changed: dict) -> bool:
    if len(changed) > _PREFILTER_MAX:
        return True
    try:
        return any(conn.execute(_MENTIONS, (tid,)).fetchone() for tid in changed)
    except sqlite3.OperationalError:  # a board without the probe table: nothing parks on probes
        return False
