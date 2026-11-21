"""A kanban notice row is always followed by a turn or by an outcome. [daveey]

Session-outcome contract, "Notice guarantee". A notice is written as a transcript row and a turn is started
separately, best-effort (live poller, unloaded sweeper wake, chat-closeout). Restarts lose some of those turns:
16 of 65 sidebar chats ended on a notice nobody answered (session-state audit 2026-10-29). Each pass of the
unloaded sweeper's loop therefore looks, in every profile store, for chats whose last active message is a
``kanban_notice`` older than ``GRACE_S`` with no live turn, and:

- a "blocked" notice whose card has moved on since (answered, rescheduled, done, archived) never wakes: it is
  stamped ``closeout.state = "superseded"`` and the chat's outcome is refreshed. Superseded user notices are
  transparent to outcome derivation and candidate discovery only with a content-bound whole-row source proof,
  so an older unanswered notice keeps its custody without hiding mixed batches. A legacy stamp can acquire
  that proof lazily; an unprovable row remains visible;
- a creator-decision notice is superseded only with an exact local source-event/render match and proof its
  card left the chat wait (a scheduled card must explicitly name a different producer). Mixed/foreign/unknown
  rows remain unanswered;
- a notice about a card blocked on a human never wakes: its owner's outcome reads ``decision``; an incidental
  recipient with no owning obligation preserves its preceding turn's outcome through an informational marker;
- otherwise the notice row is claimed (``display_metadata.closeout``, compare-and-swap on the row, so exactly one
  backend acts on it) and ONE close-out turn is started with ``server._wake_session_turn``; the claim records the
  verdict;
- a chat whose close-out budget (``WAKES_PER_HOUR`` started close-outs in its lineage) is spent, or whose wake did
  not start, gets a refresh instead, which stores ``unresolved{notice_tail}``. The auditor repairs those.
- a wake that failed (``"error"``: the agent build hit a quota wall, the store refused a write) is not final: the row
  is stamped ``closeout = {state: "deferred", retry_at, attempts}`` and offered again once ``retry_at`` passes (the
  build failure's recorded ``failure_resets_at`` plus ``DEFER_SLACK_S``, so the in-process automation replay at the
  reset goes first), at most ``MAX_ATTEMPTS`` wakes per row; the last failure stamps ``error``. The replay's timer
  dies with its backend and its own failure gives up, so before this a close-out refused by a quota window that a
  restart (or a still-broken store) outlived was never offered again (two hub chats, 2026-11-10). A deferred row
  is durable: any backend's pass picks it up. Long wall bookings include the process's frozen code identity:
  a different-code successor uses the booking's short-delay deadline instead. Same/unknown code and legacy
  rows keep the wall deadline. Failed early rechecks refund their attempt so deploys cannot exhaust the
  normal failed-wake budget before the wall reset; discovery still claims with CAS. [daveey]
- a wake that found the chat ``"busy"`` (no live turn, yet admission refused it: a core update's admission hold or
  drain, a paused turn owed to its restarting backend, an exiting backend's fence) is deferred the same way,
  ``BUSY_RETRY_S`` doubling up to ``BUSY_RETRY_MAX_S``, at most ``MAX_BUSY_ATTEMPTS`` busy wakes per row (counted in
  ``busy_attempts``, apart from the failed-wake budget); the last one stamps ``busy``. Before this ``busy`` was
  final: a notice that landed while a hub core update held admission for 30 min stayed ``unresolved{notice_tail}``
  after the hold lifted (two hub chats, 2026-11-12). A ``busy`` stamp without ``busy_attempts`` (that older core's)
  within ``LOOKBACK_S`` is offered again as busy wake 2, so those rows resolve once this ships. [daveey]
- a busy wake refused only because the core update holds admission charges no attempt: the row is stamped
  ``closeout = {state: "deferred", hold_wait: true}``, due (without ``GRACE_S``) the moment the hold lifts, and
  ``notice_hold_wait``'s watcher runs a pass within seconds of that. The sweep's passive rows for such wakes carry the
  same stamp. A busy wake whose chat is held by a backend being replaced retries in 15 s, also uncharged. [daveey]

Ownership follows the unloaded sweeper's: a chat another live backend holds is that backend's (its own pass
handles it); a chat with a live turn is skipped. The close-out prompt row is itself a notice, stamped as claimed,
so a close-out never wakes another one. Under the host loop lease (``kanban_unloaded``, design t_01a1d45d75851804
§2b) the full pass runs in the lease holder only; every other backend runs it with ``only=`` its own live session
ids (an indexed lookup of those chats, not a scan), so a chat live there is still closed out by it.

A notice about a card on another gateway (``gateway`` in its metadata, or only ``remote:`` event keys) is judged
by the ``blocked_on_human`` flag its writer stored, never by a local board lookup: its ``board`` is the owner's.
A candidate whose handling raises is stamped ``closeout.state = "error"`` once and left for the auditor, instead
of being retried (and logged) on every pass.

A notice that only refreshes (blocked on a human, spent budget) is not stamped, so it stays a candidate; each
backend refreshes it once per notice row (``_refreshed``) instead of re-deriving the same outcome every 30 s pass.
A card move still refreshes through ``notice_fanout.card_change_pass``, a newer message makes a new candidate row,
and a refresh that failed is retried. [daveey]
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from tui_gateway.kanban_notice_session_store import notice_writer, reader_of
from tui_gateway.wake_slots import WakeSlots

logger = logging.getLogger(__name__)

GRACE_S = 300.0
WAKES_PER_HOUR = 2
# A failed close-out wake is offered again (see module doc): at most this many wakes per notice row, the next one
# no sooner than the failure's quota/route reset (or the automation replay's floor) plus this slack.
MAX_ATTEMPTS = 3
DEFER_SLACK_S = 600.0
# A busy wake is offered again after BUSY_RETRY_S, doubling to BUSY_RETRY_MAX_S: 8 wakes over ~4.25 h per row.
BUSY_RETRY_S = 300.0
BUSY_RETRY_MAX_S = 3600.0
MAX_BUSY_ATTEMPTS = 8
# [daveey] Short retries of a wake whose chat a backend being replaced holds; past them, ordinary busy accounting.
MAX_RETIRING_RETRIES = 10
LOOKBACK_S = 3 * 86400.0
MAX_PARALLEL_WAKES = 2
CLOSEOUT_TEXT = (
    "[Kanban close-out] The Kanban notice above reached this chat and no turn followed it. Act on it now: check "
    "what the card delivered or why it stopped, fix or follow up on anything an agent can, then end with a short "
    "summary for the user, the decision you need from them, or what you are still waiting on.")
CLOSEOUT_DISPLAY = "Close-out turn for the Kanban notice above"
_wake_slots = WakeSlots(MAX_PARALLEL_WAKES)
# (store path, session id, notice row id, branch) this backend already refreshed. Pruned to the current
# candidates after every pass, so it holds at most one pass's candidates.
_refreshed: set[tuple[str, str, int, str]] = set()


def _reset_refreshed_for_tests() -> None:
    _refreshed.clear()

_CANDIDATES_SQL = """
SELECT s.id AS sid, m.id AS msg_id, m.role, m.timestamp, m.content, m.display_kind, m.display_metadata,
       {last_active} AS last_active
  FROM sessions s
  JOIN messages m ON m.id = (SELECT id FROM messages WHERE session_id = s.id AND active = 1
                               AND role IN ('user', 'assistant', 'tool') ORDER BY id DESC LIMIT 1)
 WHERE s.archived = 0 AND COALESCE(s.muted, 0) = 0
   AND COALESCE(s.end_reason, '') <> 'compression' AND COALESCE(s.end_reason, '') NOT LIKE 'moved_to_gateway:%'
   AND m.display_kind = 'kanban_notice'
"""

# Same rows as ``_CANDIDATES_SQL``, driven from the latest kanban-notice row of each session (the partial index
# ``idx_messages_kanban_notice``) instead of the last message of every session; ``m.id = (...)`` keeps the
# "a notice is the chat's last active message" test. 5.5x less work on a 1.3M-message store.
_CANDIDATES_INDEXED_SQL = """
SELECT s.id AS sid, m.id AS msg_id, m.role, m.timestamp, m.content, m.display_kind, m.display_metadata,
       {last_active} AS last_active
  FROM (SELECT session_id, MAX(id) AS nid FROM messages INDEXED BY idx_messages_kanban_notice
         WHERE display_kind = 'kanban_notice' AND active = 1 AND role IN ('user', 'assistant', 'tool')
         GROUP BY session_id) n
  JOIN sessions s ON s.id = n.session_id
  JOIN messages m ON m.id = n.nid
 WHERE s.archived = 0 AND COALESCE(s.muted, 0) = 0
   AND COALESCE(s.end_reason, '') <> 'compression' AND COALESCE(s.end_reason, '') NOT LIKE 'moved_to_gateway:%'
   AND m.id = (SELECT id FROM messages WHERE session_id = s.id AND active = 1
                 AND role IN ('user', 'assistant', 'tool') ORDER BY id DESC LIMIT 1)
"""


def candidates_sql(db: Any) -> str:
    """The indexed form when the store has ``idx_messages_kanban_notice`` (a build older than this one, or a
    store not yet reconciled by a writer, does not: the legacy form is the same rows, only slower)."""
    try:
        with db._read_ctx() as conn:
            has = any(r[1] == 'idx_messages_kanban_notice' for r in conn.execute('PRAGMA index_list(messages)'))
    except Exception:  # health: allow BLE001 -- the legacy scan is always correct
        return _CANDIDATES_SQL
    return _CANDIDATES_INDEXED_SQL if has else _CANDIDATES_SQL


_STARTED_SQL = """
SELECT COUNT(*) FROM messages WHERE session_id IN (SELECT id FROM lineage) AND display_kind = 'kanban_notice'
   AND json_valid(display_metadata) AND json_extract(display_metadata, '$.closeout.state') = 'started'
   AND json_extract(display_metadata, '$.closeout.at') > ?
"""


def candidates(db: Any, now: float, only: Optional[set[str]] = None) -> list[dict]:
    """Chats in ``db`` whose last active message is a kanban notice older than ``GRACE_S`` and not yet claimed;
    with ``only``, just those session ids (a non-holder's residual pass)."""
    from hermes_state_common import _sql_session_last_active
    from tui_gateway.notice_candidates import rows as candidate_rows
    from tui_gateway.notice_fanout import _decode
    db = reader_of(db)  # the kept handle: a stable cache key (``notice_candidates`` keys on the reader object)
    # ``only`` is an indexed lookup of a few chats: the legacy form already seeks them by id.
    sql = (_CANDIDATES_SQL if only is not None else candidates_sql(db)).format(last_active=_sql_session_last_active("s"))
    if only is not None:
        ids = sorted(only)
        if not ids:
            return []
        rows = db._read_all(sql + f" AND s.id IN ({','.join('?' * len(ids))})", tuple(ids))
    else:
        rows = candidate_rows(db, sql)
    out = []
    from tui_gateway.notice_history import preceding_candidate, settled_notice, whole_row_settled
    from tui_gateway.notice_hold_wait import is_hold_wait, update_hold_active
    hold: list = []  # read once per call, only when a hold-wait row is seen

    def hold_lifted() -> bool:
        if not hold:
            hold.append(update_hold_active())
        return not hold[0]

    for row in rows:
        # Expire the effective obligation, not a settled physical tail: an older
        # notice beneath it may hold a durable retry beyond the discovery window.
        row = preceding_candidate(db, row)
        # Cache structure, not age: a notice can become due (or expire) without a commit.
        meta = _decode(row["display_metadata"])
        # [daveey] A notice refused only by the update hold is due the moment the hold is gone (notice_hold_wait).
        lifted = is_hold_wait(meta) and hold_lifted()
        deferred_due = lifted or _deferred_due(meta, now)
        # Discovery lookback must not expire a durable retry during a long quota reset.
        if row["last_active"] is None or (row["last_active"] < now - LOOKBACK_S and not deferred_due):
            continue
        if not isinstance(row["timestamp"], (int, float)):
            continue
        if row["timestamp"] > now - GRACE_S and not lifted:
            continue
        cand = {"sid": str(row["sid"]), "msg_id": int(row["msg_id"]), "content": str(row["content"] or ""),
                "meta": meta, "raw_meta": row["display_metadata"]}
        closeout = meta.get("closeout")
        reprovable = (isinstance(closeout, dict) and closeout.get("state") == "superseded"
                      and not settled_notice(row) and whole_row_settled(cand))
        if "closeout" not in meta or deferred_due or _legacy_busy(meta) or reprovable:
            out.append(cand)
    return out


def _deferred_due(meta: dict, now: float) -> bool:
    """A row whose failed close-out was deferred (``closeout.state == "deferred"``) and whose ``retry_at`` passed."""
    closeout = meta.get("closeout")
    if not isinstance(closeout, dict) or closeout.get("state") != "deferred":
        return False
    from tui_gateway.notice_retry import effective_due
    retry_at = effective_due(closeout)
    return isinstance(retry_at, (int, float)) and not isinstance(retry_at, bool) and retry_at <= now


def _legacy_busy(meta: dict) -> bool:
    """A row stamped final ``busy`` by a core from before busy wakes were deferred (no ``busy_attempts``): that core
    gave up after one busy wake, so the row is offered again as its first deferred busy wake (within LOOKBACK_S)."""
    closeout = meta.get("closeout")
    return isinstance(closeout, dict) and closeout.get("state") == "busy" and "busy_attempts" not in closeout


def _attempts(meta: Any, field: str = "attempts") -> int:
    closeout = meta.get("closeout") if isinstance(meta, dict) else None
    n = closeout.get(field) if isinstance(closeout, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def started_last_hour(db: Any, sid: str, now: float) -> int:
    from hermes_state_sessions import _LINEAGE_CTE_SQL
    return int(db._read_one(_LINEAGE_CTE_SQL + _STARTED_SQL, (sid, sid, now - 3600.0))[0])


def _busy_attempts(meta: Any) -> int:
    return max(_attempts(meta, "busy_attempts"), 1 if isinstance(meta, dict) and _legacy_busy(meta) else 0)


def _claim(db: Any, cand: dict, now: float) -> bool:
    busy = _busy_attempts(cand["meta"])
    retiring = _attempts(cand["meta"], "retiring_attempts")
    meta = {**cand["meta"], "closeout": {"state": "claimed", "at": now, "pid": os.getpid(),
                                         "attempts": _attempts(cand["meta"]) + 1,
                                         **({"busy_attempts": busy} if busy else {}),
                                         **({"retiring_attempts": retiring} if retiring else {})}}
    with notice_writer(db) as wdb:
        return wdb._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                                   (json.dumps(meta, ensure_ascii=False), cand["msg_id"], cand["raw_meta"])) == 1


def _stamp(db: Any, msg_id: int, verdict: str, **extra: Any) -> None:
    from tui_gateway.notice_fanout import _decode
    with notice_writer(db) as wdb:
        row = wdb._read_one("SELECT display_metadata FROM messages WHERE id = ?", (msg_id,))
        meta = _decode(row["display_metadata"] if row else None)
        meta["closeout"] = {**(meta.get("closeout") or {}), **extra, "state": verdict}
        wdb._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ?",
                            (json.dumps(meta, ensure_ascii=False), msg_id))


def _this_gateway() -> str:
    try:
        from hermes_cli.kanban_origin import gateway_names
        return gateway_names()[0]
    except Exception:
        logger.debug("notice guarantee: this gateway's name is unknown", exc_info=True)
        return ""


def _is_remote(meta: dict) -> bool:
    """A notice about a card on another gateway: its ``board`` names the OWNER's board, not one here. [daveey]"""
    gateway = str(meta.get("gateway") or "")
    if gateway and gateway != _this_gateway():
        return True
    keys = [str(k) for k in meta.get("kanban_event_keys") or ()]
    return bool(keys) and all(k.startswith("remote:") for k in keys)


def blocked_on_human(cand: dict) -> bool:
    """True when a card this notice is about is (still) parked on a validated human request.

    Remote notices (defect kanban-a9b5d2c7c1) read the ``blocked_on_human`` flag their writer stored and never open
    a local board. A local notice whose board is gone or archived reads as not blocked, so its close-out runs."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from tui_gateway.kanban_unloaded import _blocked_on_human
    from tui_gateway.notice_fanout import _notice_tasks
    meta = cand["meta"]
    flagged = bool(meta.get("blocked_on_human"))
    if _is_remote(meta):
        return flagged
    keys = [str(k) for k in meta.get("kanban_event_keys") or () if ":" in str(k) and not str(k).startswith("remote:")]
    board = meta.get("board") or (keys[0].rsplit(":", 1)[0] if keys else None)
    tasks = _notice_tasks(cand["content"], meta)
    if not tasks:
        return flagged
    try:
        conn = _kbc.connect(board=board) if board else _kbc.connect()
    except ValueError:  # board deleted or archived since the notice: nothing here can be waiting on a human
        logger.debug("notice guarantee: board %r of notice %s is gone", board, cand.get("msg_id"))
        return flagged
    with contextlib.closing(conn):
        return flagged or any(_blocked_on_human(_kb.get_task(conn, tid)) for tid in sorted(tasks))


def superseded(cand: dict) -> bool:
    """True when this is a "blocked" notice whose card has moved on since: answered and unblocked, rescheduled,
    finished or archived. Its question needs no answer any more, so no close-out turn re-asks it (coach pattern
    closeout-notice-stale-decision: a 3-day-old "Decision needed" notice woke a turn after the card had resolved
    it itself). Local notices are judged by the live board (every card on a blocked line must be found and out of
    blocked/triage); remote notices, and local ones whose board is gone, by the ``stale`` flag the fan-out sets
    when the card changes (``notice_fanout.mark_stale_blocked_notices``). [daveey]"""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from tui_gateway.notice_fanout import _BLOCKED_LINE
    meta = cand["meta"]
    stale = bool(meta.get("stale"))  # only ever set on blocked notices, by the fan-out
    blocked = sorted(set(_BLOCKED_LINE.findall(cand["content"])))
    from tui_gateway.notice_history import creator_superseded
    if not blocked:
        return stale or (not _is_remote(meta) and creator_superseded(cand))
    if _is_remote(meta):
        return stale
    keys = [str(k) for k in meta.get("kanban_event_keys") or () if ":" in str(k) and not str(k).startswith("remote:")]
    board = meta.get("board") or (keys[0].rsplit(":", 1)[0] if keys else None)
    try:
        conn = _kbc.connect(board=board) if board else _kbc.connect()
    except ValueError:
        return stale
    with contextlib.closing(conn):
        tasks = [_kb.get_task(conn, tid) for tid in blocked]
    if any(t is None for t in tasks):
        return stale
    return all(str(getattr(t, "status", "") or "") not in ("blocked", "triage") for t in tasks)


def _supersede(db: Any, cand: dict, now: float) -> str:
    """Stamp the notice ``superseded`` (compare-and-swap like a claim) and refresh the chat's outcome."""
    from tui_gateway.notice_history import content_digest, whole_row_settled

    closeout = {"state": "superseded", "at": now, "pid": os.getpid()}
    if whole_row_settled(cand):
        closeout["settled_content_sha256"] = content_digest(cand["content"])
    meta = {**cand["meta"], "closeout": closeout}
    with notice_writer(db) as wdb:
        if wdb._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                               (json.dumps(meta, ensure_ascii=False), cand["msg_id"], cand["raw_meta"])) != 1:
            return "skipped:claimed"
        _refresh(wdb, cand["sid"])
    return "superseded"


def _wake(sid: str, msg_id: int) -> str:
    from tui_gateway import server
    meta = {"display_text": CLOSEOUT_DISPLAY, "closeout": {"state": "self", "of": msg_id}}
    return server._wake_session_turn(sid, CLOSEOUT_TEXT, display_kind="kanban_notice", display_metadata=meta,
                                     label="notice-closeout")


def _spawn(job: Callable[[], Any]) -> None:
    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(job, name="notice-closeout").start()


def _refresh(db: Any, sid: str) -> bool:
    """Refresh one chat's outcome; True when it refreshed (``refresh_outcomes_quietly`` logs and swallows a failure)."""
    import hermes_state_outcome as _outcome
    with notice_writer(db) as wdb:
        return _outcome.refresh_outcomes_quietly([sid], "notice", db=wdb) > 0


def _retry_at(sid: str, now: float) -> float:
    """When a failed close-out of ``sid`` may be offered again: the automation replay's due time for the chat's
    recorded build failure (its ``failure_resets_at`` when it is a typed quota/route wall) plus ``DEFER_SLACK_S``."""
    from tui_gateway import automation_replay
    resets_at = None
    with contextlib.suppress(Exception):
        from tui_gateway import server
        live = server._find_live_session_by_key(sid)
        failure = live[1].get("agent_failure") if live else None
        resets_at = failure.get("failure_resets_at") if isinstance(failure, dict) else None
    return automation_replay.replay_due(now, automation_replay.trusted_resets_at(resets_at)) + DEFER_SLACK_S


def _closeout_job(session_db, wake, sid: str, msg_id: int, attempts: int = 1,
                  retry_at: Optional[Callable[[str, float], float]] = None, busy_attempts: int = 0,
                  early_recheck: bool = False, retiring_attempts: int = 0) -> str:
    """Worker: the wake, then the verdict on the claimed row; a wake that did not start refreshes the outcome.
    A failed (``"error"``) or busy wake with attempts left is stamped ``deferred`` for a later pass (module doc)."""
    try:
        try:
            verdict = str(wake(sid, msg_id) or "error")
        except Exception:
            logger.warning("notice close-out wake of %s failed", sid, exc_info=True)
            verdict = "error"
        extra: dict = {}
        if verdict == "error" and early_recheck:
            attempts -= 1  # Speculative successor failure must leave the reset-time budget intact.
        if verdict == "busy" and _held_by_update():
            # [daveey] Refused only by the core update's admission hold: no busy attempt and no failed-wake attempt
            # is charged; the row is due as soon as the hold lifts and the watcher wakes it then (notice_hold_wait).
            from tui_gateway.notice_hold_wait import hold_wait_closeout, note_waiting
            extra = {**hold_wait_closeout(), "attempts": max(0, attempts - 1), "busy_attempts": busy_attempts}
            verdict = "deferred"
            with contextlib.suppress(Exception):
                note_waiting()
        elif (verdict == "busy" and (soon := _retiring_retry_at(sid)) is not None
              and retiring_attempts < MAX_RETIRING_RETRIES):
            # [daveey] The chat's owner is a backend being replaced: it lets go within seconds. A short retry, and no
            # attempt charged (gateway_background_holder.wake_past_retiring_owner), up to MAX_RETIRING_RETRIES.
            extra = {"retry_at": soon, "attempts": max(0, attempts - 1), "busy_attempts": busy_attempts,
                     "retiring_attempts": retiring_attempts + 1}
            verdict = "deferred"
        elif verdict == "busy":
            busy_attempts += 1
            extra = {"busy_attempts": busy_attempts}
            if busy_attempts < MAX_BUSY_ATTEMPTS:
                delay = min(BUSY_RETRY_S * 2 ** (busy_attempts - 1), BUSY_RETRY_MAX_S)
                extra["retry_at"] = time.time() + delay
                verdict = "deferred"
        elif verdict == "error" and attempts - busy_attempts < MAX_ATTEMPTS:
            now = time.time()
            try:
                from tui_gateway.automation_replay import replay_due
                from tui_gateway.notice_retry import booking
                extra = {"retry_at": float((retry_at or _retry_at)(sid, now)), "attempts": attempts}
                extra.update(booking(extra["retry_at"], replay_due(now) + DEFER_SLACK_S))
                verdict = "deferred"
            except Exception:
                logger.warning("notice close-out of %s: no retry time; stamped error", sid, exc_info=True)
        with session_db(sid) as (db, tip):
            if db is not None:
                _stamp(db, msg_id, verdict, **extra)
                if verdict != "started":
                    _refresh(db, tip or sid)
        logger.info("notice close-out of %s: %s", sid, verdict)
        return verdict
    finally:
        _wake_slots.release()


def _retiring_retry_at(sid: str) -> Optional[float]:
    from tui_gateway.gateway_background_holder import retiring_retry_at
    return retiring_retry_at(sid)


def _held_by_update() -> bool:
    from tui_gateway.notice_hold_wait import update_hold_active
    return update_hold_active()


def _refresh_key(db: Any, cand: dict, branch: str) -> tuple[str, str, int, str]:
    return (str(getattr(db, "db_path", id(db))), cand["sid"], cand["msg_id"], branch)


def _refresh_once(db: Any, cand: dict, branch: str, seen: set) -> str:
    """Refresh the chat's outcome for this notice row once per backend; later passes skip it."""
    key = _refresh_key(db, cand, branch)
    seen.add(key)
    if key in _refreshed:
        return "skipped:refreshed"
    from hermes_state_outcome import get_outcome

    with notice_writer(db) as wdb:  # one writer borrow for the refresh, its readback and the marker
        refreshed = _refresh(wdb, cand["sid"])
        outcome = get_outcome(cand["sid"], db=wdb) if refreshed else None
        if branch == "human" and outcome and outcome.get("reason") == "notice_tail":
            # No owning decision/wait accounted for this intentionally passive notice.
            # Preserve the preceding turn instead of manufacturing an obligation for
            # an incidental recipient. Other outcomes need no metadata write.
            meta = {**cand["meta"], "outcome_informational": True}
            changed = wdb._write_rowcount(
                "UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                (json.dumps(meta, ensure_ascii=False), cand["msg_id"], cand["raw_meta"]))
            if changed != 1:
                return "skipped:changed"
            refreshed = _refresh(wdb, cand["sid"])
    if refreshed:
        _refreshed.add(key)
    return f"refreshed:{branch}"


def _handle(db, cand: dict, now: float, *, holders, session_db, wake, spawn, seen: Optional[set] = None) -> str:
    from hermes_state_outcome import _live_turn
    from tui_gateway.kanban_unloaded import _other_holder
    sid = cand["sid"]
    seen = set() if seen is None else seen
    if _other_holder(holders, {sid}):
        return "skipped:held"
    if _live_turn(db, sid):
        return "skipped:live"
    if superseded(cand):
        return _supersede(db, cand, now)
    if blocked_on_human(cand):
        return _refresh_once(db, cand, "human", seen)
    if started_last_hour(db, sid, now) >= WAKES_PER_HOUR:
        return _refresh_once(db, cand, "budget", seen)
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
        attempts = _attempts(cand["meta"]) + 1
        busy = _busy_attempts(cand["meta"])
        from tui_gateway.notice_retry import early_recheck
        early = early_recheck(cand["meta"], now)
        retiring = _attempts(cand["meta"], "retiring_attempts")
        spawn(lambda: _closeout_job(session_db, wake, sid, cand["msg_id"], attempts=attempts,
                                   busy_attempts=busy, early_recheck=early, retiring_attempts=retiring))
    except Exception:
        _wake_slots.release()
        _stamp(db, cand["msg_id"], "error")
        _refresh(db, sid)
        logger.warning("notice close-out for %s could not start", sid, exc_info=True)
        return "error"
    return "dispatched"


def _give_up(db, cand: dict) -> str:
    """A candidate whose handling raised: stamp it ``error`` (compare-and-swap, so a row another backend claimed
    meanwhile is left alone) and refresh the outcome, which then reads ``unresolved{notice_tail}`` for the auditor.
    Without the stamp the same row was retried and logged on every pass forever (349 warnings in one window). [daveey]"""
    try:
        meta = {**cand["meta"], "closeout": {"state": "error", "at": time.time(), "pid": os.getpid()}}
        with notice_writer(db) as wdb:
            wdb._write_rowcount("UPDATE messages SET display_metadata = ? WHERE id = ? AND display_metadata IS ?",
                                (json.dumps(meta, ensure_ascii=False), cand["msg_id"], cand["raw_meta"]))
            _refresh(wdb, cand["sid"])
    except Exception:
        logger.debug("notice guarantee: could not stamp %s as error", cand["sid"], exc_info=True)
    return "error"


@contextlib.contextmanager
def _profile_store(profile: Optional[str]) -> Iterator[Any]:
    """The launch profile's shared writable store; a foreign profile's kept read-only store (no per-pass writer
    open or schema reconcile: that store belongs to its own gateway). Notice writes and refreshes borrow the
    shared writer through ``notice_writer``. [daveey]"""
    from tui_gateway import server
    home = server._profile_home(profile) if profile else None
    if home is None:
        with server._profile_db(None, writer=True) as db:
            yield db
        return
    from tui_gateway.kanban_notice_session_store import profile_store
    with profile_store(Path(home) / "state.db") as store:
        yield store


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
                   wake: Callable[[str, int], str] = _wake, spawn: Callable[[Callable], Any] = _spawn,
                   only: Optional[set[str]] = None) -> list[dict]:
    """One pass over every store (see module doc). Returns ``[{session_id, msg_id, result}]``. Never raises.
    ``only``: just these live session ids (a non-holder's residual pass; the refresh memo is left alone)."""
    from tui_gateway import kanban_unloaded as ku
    now = time.time() if now is None else now
    holders = holders or ku.registry_holders
    session_db = session_db or ku.find_session_db
    out: list[dict] = []
    seen: set = set()
    try:
        for db in stores():
            for cand in candidates(db, now, only):
                try:
                    result = _handle(db, cand, now, holders=holders, session_db=session_db, wake=wake, spawn=spawn,
                                     seen=seen)
                except Exception:
                    logger.warning("notice guarantee failed for %s", cand["sid"], exc_info=True)
                    result = _give_up(db, cand)
                out.append({"session_id": cand["sid"], "msg_id": cand["msg_id"], "result": result})
    except Exception:
        logger.warning("notice guarantee pass failed", exc_info=True)
        return out  # an incomplete pass keeps every memo entry
    if only is None:
        _refreshed.intersection_update(seen)
    return out
