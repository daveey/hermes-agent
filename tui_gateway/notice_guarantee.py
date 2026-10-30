"""A kanban notice row is always followed by a turn or by an outcome. [daveey]

Session-outcome contract, "Notice guarantee". A notice is written as a transcript row and a turn is started
separately, best-effort (live poller, unloaded sweeper wake, chat-closeout). Restarts lose some of those turns:
16 of 65 sidebar chats ended on a notice nobody answered (session-state audit 2026-10-29). Each pass of the
unloaded sweeper's loop therefore looks, in every profile store, for chats whose last active message is a
``kanban_notice`` older than ``GRACE_S`` with no live turn, and:

- a notice about a card blocked on a human never wakes: the chat's outcome is refreshed (it reads ``decision``);
- otherwise the notice row is claimed (``display_metadata.closeout``, compare-and-swap on the row, so exactly one
  backend acts on it) and ONE close-out turn is started with ``server._wake_session_turn``; the claim records the
  verdict;
- a chat whose close-out budget (``WAKES_PER_HOUR`` started close-outs in its lineage) is spent, or whose wake did
  not start, gets a refresh instead, which stores ``unresolved{notice_tail}``. The auditor repairs those.

Ownership follows the unloaded sweeper's: a chat another live backend holds is that backend's (its own pass
handles it); a chat with a live turn is skipped. The close-out prompt row is itself a notice, stamped as claimed,
so a close-out never wakes another one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

GRACE_S = 300.0
WAKES_PER_HOUR = 2
LOOKBACK_S = 3 * 86400.0
MAX_PARALLEL_WAKES = 2
CLOSEOUT_TEXT = (
    "[Kanban close-out] The Kanban notice above reached this chat and no turn followed it. Act on it now: check "
    "what the card delivered or why it stopped, fix or follow up on anything an agent can, then end with a short "
    "summary for the user, the decision you need from them, or what you are still waiting on.")
CLOSEOUT_DISPLAY = "Close-out turn for the Kanban notice above"
_wake_slots = threading.BoundedSemaphore(MAX_PARALLEL_WAKES)

_CANDIDATES_SQL = """
SELECT s.id AS sid, m.id AS msg_id, m.timestamp, m.content, m.display_metadata
  FROM sessions s
  JOIN messages m ON m.id = (SELECT id FROM messages WHERE session_id = s.id AND active = 1
                               AND role IN ('user', 'assistant', 'tool') ORDER BY id DESC LIMIT 1)
 WHERE s.archived = 0 AND COALESCE(s.muted, 0) = 0
   AND COALESCE(s.end_reason, '') <> 'compression' AND COALESCE(s.end_reason, '') NOT LIKE 'moved_to_gateway:%'
   AND {last_active} >= ? AND m.display_kind = 'kanban_notice' AND m.timestamp <= ?
"""

_STARTED_SQL = """
SELECT COUNT(*) FROM messages WHERE session_id IN (SELECT id FROM lineage) AND display_kind = 'kanban_notice'
   AND json_valid(display_metadata) AND json_extract(display_metadata, '$.closeout.state') = 'started'
   AND json_extract(display_metadata, '$.closeout.at') > ?
"""


def candidates(db: Any, now: float) -> list[dict]:
    """Chats in ``db`` whose last active message is a kanban notice older than ``GRACE_S`` and not yet claimed."""
    from hermes_state_common import _sql_session_last_active
    from tui_gateway.notice_fanout import _decode
    rows = db._read_all(_CANDIDATES_SQL.format(last_active=_sql_session_last_active("s")),
                        (now - LOOKBACK_S, now - GRACE_S))
    out = []
    for row in rows:
        meta = _decode(row["display_metadata"])
        if "closeout" not in meta:
            out.append({"sid": str(row["sid"]), "msg_id": int(row["msg_id"]), "content": str(row["content"] or ""),
                        "meta": meta, "raw_meta": row["display_metadata"]})
    return out


def started_last_hour(db: Any, sid: str, now: float) -> int:
    from hermes_state_sessions import _LINEAGE_CTE_SQL
    return int(db._read_one(_LINEAGE_CTE_SQL + _STARTED_SQL, (sid, sid, now - 3600.0))[0])


def _claim(db: Any, cand: dict, now: float) -> bool:
    meta = {**cand["meta"], "closeout": {"state": "claimed", "at": now, "pid": os.getpid()}}
    return db._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                              (json.dumps(meta, ensure_ascii=False), cand["msg_id"], cand["raw_meta"])) == 1


def _stamp(db: Any, msg_id: int, verdict: str) -> None:
    from tui_gateway.notice_fanout import _decode
    row = db._read_one("SELECT display_metadata FROM messages WHERE id = ?", (msg_id,))
    meta = _decode(row["display_metadata"] if row else None)
    meta["closeout"] = {**(meta.get("closeout") or {}), "state": verdict}
    db._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ?",
                       (json.dumps(meta, ensure_ascii=False), msg_id))


def blocked_on_human(cand: dict) -> bool:
    """True when a card this notice is about is (still) parked on a validated human request."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from tui_gateway.kanban_unloaded import _blocked_on_human
    from tui_gateway.notice_fanout import _notice_tasks
    meta = cand["meta"]
    keys = [str(k) for k in meta.get("kanban_event_keys") or () if ":" in str(k) and not str(k).startswith("remote:")]
    board = meta.get("board") or (keys[0].rsplit(":", 1)[0] if keys else None)
    tasks = _notice_tasks(cand["content"], meta)
    if not tasks:
        return False
    with contextlib.closing(_kbc.connect(board=board) if board else _kbc.connect()) as conn:
        return any(_blocked_on_human(_kb.get_task(conn, tid)) for tid in sorted(tasks))


def _wake(sid: str, msg_id: int) -> str:
    from tui_gateway import server
    meta = {"display_text": CLOSEOUT_DISPLAY, "closeout": {"state": "self", "of": msg_id}}
    return server._wake_session_turn(sid, CLOSEOUT_TEXT, display_kind="kanban_notice", display_metadata=meta,
                                     label="notice-closeout")


def _spawn(job: Callable[[], Any]) -> None:
    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(job, name="notice-closeout").start()


def _refresh(db: Any, sid: str) -> None:
    from hermes_state_outcome import refresh_outcomes_quietly
    refresh_outcomes_quietly([sid], "notice", db=db)


def _closeout_job(session_db, wake, sid: str, msg_id: int) -> str:
    """Worker: the wake, then the verdict on the claimed row; a wake that did not start refreshes the outcome."""
    try:
        try:
            verdict = str(wake(sid, msg_id) or "error")
        except Exception:
            logger.warning("notice close-out wake of %s failed", sid, exc_info=True)
            verdict = "error"
        with session_db(sid) as (db, tip):
            if db is not None:
                _stamp(db, msg_id, verdict)
                if verdict != "started":
                    _refresh(db, tip or sid)
        logger.info("notice close-out of %s: %s", sid, verdict)
        return verdict
    finally:
        _wake_slots.release()


def _handle(db, cand: dict, now: float, *, holders, session_db, wake, spawn) -> str:
    from hermes_state_outcome import _live_turn
    from tui_gateway.kanban_unloaded import _other_holder
    sid = cand["sid"]
    if _other_holder(holders, {sid}):
        return "skipped:held"
    if _live_turn(db, sid):
        return "skipped:live"
    if blocked_on_human(cand):
        _refresh(db, sid)
        return "refreshed:human"
    if started_last_hour(db, sid, now) >= WAKES_PER_HOUR:
        _refresh(db, sid)
        return "refreshed:budget"
    if not _wake_slots.acquire(blocking=False):
        return "skipped:slots"
    try:
        claimed = _claim(db, cand, now)
    except Exception:
        _wake_slots.release()
        raise
    if not claimed:
        _wake_slots.release()
        return "skipped:claimed"
    try:
        spawn(lambda: _closeout_job(session_db, wake, sid, cand["msg_id"]))
    except Exception:
        _wake_slots.release()
        _stamp(db, cand["msg_id"], "error")
        _refresh(db, sid)
        logger.warning("notice close-out for %s could not start", sid, exc_info=True)
        return "error"
    return "dispatched"


@contextlib.contextmanager
def _profile_store(profile: Optional[str]) -> Iterator[Any]:
    from tui_gateway import server
    with server._profile_db({"profile": profile} if profile else None, writer=True) as db:
        yield db


def _stores() -> Iterator[Any]:
    from tui_gateway import server
    for profile in server._readopt_wake_profiles():
        try:
            with _profile_store(profile) as db:
                if db is not None:
                    yield db
        except Exception:
            logger.debug("notice guarantee: profile %s store unavailable", profile, exc_info=True)


def guarantee_pass(*, now: Optional[float] = None, stores: Callable[[], Iterator[Any]] = _stores,
                   holders: Optional[Callable[[], list]] = None, session_db: Optional[Callable] = None,
                   wake: Callable[[str, int], str] = _wake, spawn: Callable[[Callable], Any] = _spawn) -> list[dict]:
    """One pass over every store (see module doc). Returns ``[{session_id, msg_id, result}]``. Never raises."""
    from tui_gateway import kanban_unloaded as ku
    now = time.time() if now is None else now
    holders = holders or ku.registry_holders
    session_db = session_db or ku.find_session_db
    out: list[dict] = []
    try:
        for db in stores():
            for cand in candidates(db, now):
                try:
                    result = _handle(db, cand, now, holders=holders, session_db=session_db, wake=wake, spawn=spawn)
                except Exception:
                    logger.warning("notice guarantee failed for %s", cand["sid"], exc_info=True)
                    result = "error"
                out.append({"session_id": cand["sid"], "msg_id": cand["msg_id"], "result": result})
    except Exception:
        logger.warning("notice guarantee pass failed", exc_info=True)
    return out
