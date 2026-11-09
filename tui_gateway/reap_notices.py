"""Tell a parent chat when the startup orphan reap closed its subagent mid-work. [daveey]

A delegate child row that a backend restart killed while it was mid-tool or waiting on its next model call ends
``startup_orphan_reap`` with its last active message a tool or user row. Its parent never got a result (13 such
children in 14 days on the MacBook, session-state audit 2026-10-29). For each one the reap appends ONE notice to
the parent's live tip ("subagent <title> was stopped by a restart before finishing") and refreshes the parent's
outcome, which then reads ``unresolved{subagent_killed}`` until a turn answers it (the notice guarantee wakes it).

A parent mid-turn refuses the row (``SessionTurnLeaseLostError``; a compression rotating its tip refuses with
``CompressionSessionClosedError``): that is "busy, try later", logged once at INFO with no traceback. The kanban
unloaded sweeper then calls :func:`retry_pending_killed_notices` every pass, which re-offers each child reaped in
the last day whose parent lineage has no ``subagent-killed:<child>`` notice yet (``notice_id`` makes repeats a
no-op). A notice still undelivered ``OVERDUE_S`` after the reap logs ONE warning per child: that is the
real-failure line (desktop-ac1b365935: the inline write was refused by a busy parent and dropped for good).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

RETRY_WINDOW_S = 24 * 3600.0  # children reaped longer ago than this are not re-offered
OVERDUE_S = 1800.0  # a notice still undelivered this long after the reap warns once
_overdue_lock = threading.Lock()
_overdue_warned: set[str] = set()  # child ids already warned about (this process)

_REAPED_SQL = """
SELECT id, ended_at FROM sessions
 WHERE parent_session_id IS NOT NULL AND end_reason = 'startup_orphan_reap' AND ended_at >= ?
"""


def _reset_for_tests() -> None:
    with _overdue_lock:
        _overdue_warned.clear()

_CHILDREN_SQL = """
SELECT s.id, s.title, s.parent_session_id,
       (SELECT role FROM messages WHERE session_id = s.id AND active = 1 AND role IN ('user', 'assistant', 'tool')
         ORDER BY id DESC LIMIT 1) AS last_role
  FROM sessions s
 WHERE s.id IN ({marks}) AND s.parent_session_id IS NOT NULL
   AND (s.source = 'subagent' OR {delegate} IS NOT NULL)
"""


def killed_subagents(db: Any, swept: Iterable[str]) -> list[dict]:
    """The reaped rows that are delegate children stopped mid-work: ``[{id, title, parent}]``."""
    from hermes_state_common import _sql_json_extract
    ids = [s for s in dict.fromkeys(swept) if s]
    if not ids:
        return []
    sql = _CHILDREN_SQL.format(marks=",".join("?" * len(ids)),
                               delegate=_sql_json_extract("s.model_config", "$._delegate_from"))
    return [{"id": str(r["id"]), "title": r["title"] or "subagent", "parent": str(r["parent_session_id"])}
            for r in db._read_all(sql, tuple(ids)) if r["last_role"] in ("tool", "user")]


def _deliver(db: Any, child: dict) -> tuple[str, Optional[str]]:
    """Write one child's notice to its parent's live tip. ``("written"|"present"|"busy"|"error", tip)``; never
    raises. ``busy`` = the parent refused because a turn (or a compression) owns it right now."""
    from hermes_state_errors import CompressionSessionClosedError, SessionTurnLeaseLostError
    tip = child["parent"]
    try:
        tip = db.get_compression_tip(child["parent"]) or child["parent"]
        text = (f"⚠ subagent {child['title']} was stopped by a restart before finishing (child session "
                f"{child['id']}); its result never came back. Check what it got done and redo or re-delegate the rest.")
        metadata = {"notice_id": f"subagent-killed:{child['id']}", "display_text": text,
                    "subagent_killed": child["id"]}
        _row, inserted = db.record_kanban_notice(tip, text, metadata)
    except (SessionTurnLeaseLostError, CompressionSessionClosedError):
        return "busy", tip
    except Exception:
        logger.warning("could not tell parent %s that subagent %s was reaped", tip, child["id"], exc_info=True)
        return "error", tip
    return ("written" if inserted else "present"), tip


def notify_parents_of_killed_subagents(db: Any, swept: Iterable[str]) -> list[str]:
    """Append one notice per killed child to its parent's tip and refresh that parent. Returns the parents
    notified. Never raises. A parent mid-turn refuses the row: one INFO line, and the sweeper's
    :func:`retry_pending_killed_notices` delivers it once the turn ends."""
    from hermes_state_outcome import refresh_outcomes_quietly
    notified: list[str] = []
    try:
        children = killed_subagents(db, swept)
    except Exception:
        logger.warning("killed-subagent lookup after the orphan reap failed", exc_info=True)
        return notified
    for child in children:
        result, tip = _deliver(db, child)
        if result == "busy":
            logger.info("could not tell parent %s yet that subagent %s was reaped: parent busy, will retry",
                        tip, child["id"])
        elif result == "written":
            notified.append(tip)
    refresh_outcomes_quietly(notified, "reap", db=db)
    if notified:
        logger.info("orphan reap: told %d parent chat(s) their subagent was stopped mid-work: %s",
                    len(notified), ", ".join(notified))
    return notified


def retry_pending_killed_notices(db: Any, *, now: Optional[float] = None) -> list[str]:
    """Re-offer the notice for every child the orphan reap closed in the last ``RETRY_WINDOW_S`` whose parent
    lineage does not have it yet (the inline write found the parent busy). Returns the parents newly told. A
    still-busy parent is skipped quietly; ``OVERDUE_S`` after the reap it logs one WARNING per child. Already
    delivered children cost one lineage read (no write lock). Never raises."""
    from hermes_state_outcome import refresh_outcomes_quietly
    if db is None:
        return []
    now = time.time() if now is None else now
    try:
        reaped = {str(r["id"]): r for r in db._read_all(
            _REAPED_SQL, (now - RETRY_WINDOW_S,))}
        children = killed_subagents(db, list(reaped))
    except Exception:
        logger.warning("killed-subagent notice retry: lookup failed", exc_info=True)
        return []
    notified: list[str] = []
    for child in children:
        result, tip = _deliver(db, child)
        if result == "written":
            notified.append(tip)
            logger.info("orphan reap retry: told parent %s that subagent %s was stopped mid-work", tip, child["id"])
        if result in ("written", "present"):
            with _overdue_lock:
                _overdue_warned.discard(child["id"])
            continue
        age = now - float(reaped[child["id"]]["ended_at"] or now)
        with _overdue_lock:
            if age < OVERDUE_S or child["id"] in _overdue_warned:
                continue
            _overdue_warned.add(child["id"])
        logger.warning("killed-subagent notice for child %s to parent %s overdue %dm: %s", child["id"], tip,
                       age // 60, "parent busy (a turn is running in it)" if result == "busy" else "write failed")
    refresh_outcomes_quietly(notified, "reap", db=db)
    return notified
