"""A card a chat waits on directly: ranked on David's go, sized in the unblock result, watched once queued. [daveey]

Defect agent-70dc07d1a5 (2026-11-02): David said "go" in a live chat; it unblocked the ``human_request`` card
t_01a19e4974912dcf and promised "a worker will pick it up". The card stayed p0 and sat ready for 113 min behind 47
older p0 cards (the hub cap is 7). ``kanban_unblock`` returned only ``{"status": "ready"}``, so the chat could not
size the wait, and nothing told it that the card had no worker until David asked "what are we waiting for".

- :func:`raise_for_human_go`: a chat's unblock of a card that carried a ``human_request`` raises its stored
  priority to at least :data:`HUMAN_GO_PRIORITY` (never lowers it) and logs ``priority_raised`` (``human_go``).
- :func:`unblock_fields`: ``queue`` (``kanban_queue_position.position``) for the unblock result, plus
  ``queue_hint`` when at least as many cards are ahead as the host has slots.
- :func:`sweep` (each non-dry dispatcher tick): ONE ``chat_wait_unclaimed`` event per ready episode for a
  chat-owned card (a notify subscription or ``session_id``) ready and unclaimed for at least
  ``THRESHOLD_SECONDS``. The gateway and TUI notifiers deliver it to that chat, which wakes. Delivery reuses
  ``kanban_parent_unclaimed.post_notice``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from hermes_cli import kanban_last_checkpoint as last_checkpoint
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli.kanban_ready_events import conditional_ready_entry_sql

_log = logging.getLogger(__name__)

NOTICE_KIND = "chat_wait_unclaimed"
RAISE_KIND = "priority_raised"
HUMAN_GO_PRIORITY = 10
THRESHOLD_SECONDS = pu.THRESHOLD_SECONDS
# A ready episode older than this when first seen is backlog, not a wait the chat just promised; kanban_show's
# queue_hint covers it. This also bounds the burst on the first tick after deploy.
MAX_EPISODE_SECONDS = 6 * 3600
# Boards (main DB file of the swept conn) whose current lag stall has logged its WARNING; the rest of that stall logs
# at INFO. Keyed per board: the dispatcher sweeps each board separately, so a healthy board's undeferred sweep must
# not re-arm a lagging board's WARNING every tick. A stall is one run of replica lag, so a board leaves the set only on
# its own sweep that finds the lag closed: an undeferred sweep, or one with nothing due whose lag_hold is None (a
# stall that ends with no card waiting would otherwise make the next one log only at INFO). Cards that come due and
# get claimed within one lag log at INFO after the first WARNING; lag_hold runs on a nothing-due sweep only for a
# board in the set.
_LAG_WARNED: set[str] = set()
NO_PROMISE = "check placement and available capacity first; re-place if suitable; don't promise a pickup"


# --- priority on a chat's human-approval unblock ---------------------------------------------------------------


def had_human_request(conn, task_id: str) -> bool:
    """True when ``task_id`` currently carries a non-empty ``human_request`` (read before the unblock clears it)."""
    row = conn.execute("SELECT human_request FROM tasks WHERE id = ?", (task_id,)).fetchone()
    raw = row[0] if row is not None else None
    return bool(raw) and str(raw).strip() not in ("", "null", "{}")


def raise_for_human_go(conn, task_id: str) -> Optional[dict[str, int]]:
    """Raise ``task_id``'s priority to at least :data:`HUMAN_GO_PRIORITY`; ``{"from", "to"}`` when it changed."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        current = int(row[0] or 0)
        if current >= HUMAN_GO_PRIORITY:
            return None
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (HUMAN_GO_PRIORITY, task_id))
        change = {"from": current, "to": HUMAN_GO_PRIORITY}
        kb._append_event(conn, task_id, RAISE_KIND, {**change, "reason": "human_go"})
    kb.notify_task_updated(conn, task_id, ["priority"])
    return change


# --- queue place in the unblock result --------------------------------------------------------------------------


def unblock_fields(conn, task, board: Optional[str] = None) -> dict[str, Any]:
    """``{"queue": ..., ["queue_hint": ...]}`` for an unblock result; empty when the card is not ready."""
    from hermes_cli import kanban_queue_position as kqp
    place = kqp.read(conn, task, board)
    if not place:
        return {}
    out: dict[str, Any] = {"queue": place}
    ahead, limit = place.get("ahead"), place.get("limit")
    if place.get("lane") == "local" and ahead is not None and limit is not None and ahead >= limit:
        out["queue_hint"] = f"{ahead} card(s) ahead and {limit} slot(s) on this host: {NO_PROMISE}."
    elif place.get("lane") == "unplaceable":
        out["queue_hint"] = f"{kqp.describe(place)}. {NO_PROMISE}."
    return out


# --- direct chat wait notice ------------------------------------------------------------------------------------


def notice_text(payload: Any) -> str:
    """Chat line for ``chat_wait_unclaimed`` (gateway and TUI notifiers share it)."""
    payload = payload if isinstance(payload, dict) else {}
    tid = str(payload.get("parent") or "<card>")[:64]
    if payload.get("unclaimable"):
        return (f"{tid}, which this chat waits on, has been ready {payload.get('ready_minutes', '?')} min. "
                f"{payload['unclaimable']}")
    title = str(payload.get("parent_title") or "").strip()[:120]
    named = f"{tid} ({title})" if title else tid
    checkpoint = last_checkpoint.sentence(payload)  # before the advice (kanban-3debc730ab) [daveey]
    if payload.get("core_update_hold"):
        return (f"{named}, which this chat waits on, has been ready {payload.get('ready_minutes', '?')} min "
                "with no worker: claims are held for a pending Hermes update "
                f"(hold started {payload.get('core_update_started') or 'at an unknown time'}). " + checkpoint
                + "The hold lasts until the update applies or the drain's hard max.")
    if payload.get("missing_shared_board"):
        return (f"{named}, which this chat waits on, has been ready {payload.get('ready_minutes', '?')} min "
                f"with no worker because it isn't on the shared Board (claim_deferred: not_found). "
                f"Last sync rejection: {payload.get('sync_reject_reason') or 'no sync rejection recorded'}. "
                + checkpoint + "Repair the Board sync rejection before retrying dispatch.")
    if payload.get("respawn_guard"):
        return (f"{named}, which this chat waits on, has been ready {payload.get('ready_minutes', '?')} min "
                f"with no worker: last recorded respawn guard is {payload['respawn_guard']}. " + checkpoint
                + "Check the current dispatcher verdict and resolve that guard; priority changes do not clear it.")
    hold = ""
    if payload.get("hold_reason") and payload.get("hold_until"):
        stamp = time.strftime("%H:%M:%SZ", time.gmtime(int(payload["hold_until"])))
        hold = f"; dispatcher hold {payload['hold_reason']} until {stamp}"
    return (f"{named}, which this chat waits on, has been ready {payload.get('ready_minutes', '?')} min with no "
            f"worker ({payload.get('queue') or 'queue place unknown'}{hold}){pu._observation(payload)}."
            + pu._pool_suffix(payload) + " " + checkpoint + pu._options(tid, payload.get("priority_evidence"), payload)
            + ".")


def _floor_kinds(*, include_conditional: bool = True) -> str:
    from hermes_cli.kanban_ready_events import READY_ENTRY_CONDITIONAL_KINDS
    from hermes_cli.kanban_ready_events import READY_WAIT_FLOOR_KINDS as _READY_WAIT_FLOOR_KINDS
    kinds = _READY_WAIT_FLOOR_KINDS | (READY_ENTRY_CONDITIONAL_KINDS if include_conditional else frozenset())
    return ", ".join(f"'{k}'" for k in sorted(kinds))


# Prefilter only (``pu.ready_since`` decides): the same event floor as the diagnostic, and the latest run's end,
# so a long run that a restart requeued never counts as ready time. A Board grant returned unused (a released run
# with only ``{"unused_grant": true}`` metadata and no worker) is a reservation: neither its ``claimed`` event nor
# its run's end counts, or a card whose grants keep bouncing would be hidden from the exact check. [daveey]
_UNUSED_RUN = ("r.outcome = 'released' AND r.worker_pid IS NULL AND r.worker_started_at IS NULL "
               "AND json_extract(r.metadata, '$.unused_grant') = 1 AND json_extract(r.metadata, '$.lock') IS NULL")
_CANDIDATES = (
    "SELECT t.id, t.title, t.created_at FROM tasks t "
    "WHERE t.status = 'ready' AND t.claim_lock IS NULL "
    "AND (t.session_id IS NOT NULL OR EXISTS (SELECT 1 FROM kanban_notify_subs s WHERE s.task_id = t.id)) "
    f"AND MAX(COALESCE((SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = t.id AND (e.kind IN "
    f"({_floor_kinds(include_conditional=False)}) OR {conditional_ready_entry_sql('e.kind', 'e.payload')}) "
    f"AND NOT (e.kind = 'claimed' AND e.run_id IN "
    f"(SELECT r.id FROM task_runs r WHERE r.task_id = t.id AND {_UNUSED_RUN}))), t.created_at), "
    f"COALESCE((SELECT MAX(r.ended_at) FROM task_runs r WHERE r.task_id = t.id AND NOT ({_UNUSED_RUN})), 0)) "
    "BETWEEN ? AND ? ORDER BY t.id"
)


def _clock_hold(conn, task_id: str, since: int, now: float) -> dict[str, Any]:
    """``{"hold_reason", "hold_until"}`` while a clock guard still holds ``task_id``, else ``{}``.

    The reason of this ready episode's latest ``respawn_guarded`` event (at or after ``since``); a latest run
    lost to an owner restart is the owner-restart cooldown even before the tick that records the guard.
    Advisory only: a failed lookup drops the hold from the notice, never the notice."""
    try:
        return _clock_hold_unchecked(conn, task_id, since, now)
    except Exception:  # health: allow BLE001 -- the hold is advisory; the notice must still go out
        _log.debug("chat-wait: clock hold lookup failed for %s", task_id, exc_info=True)
        return {}


def _clock_hold_unchecked(conn, task_id: str, since: int, now: float) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _is_owner_restart_run
    from hermes_cli.kanban_db_dispatch_clock import guard_hold_until
    reason = None
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded' "
                       "AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 1", (task_id, int(since))).fetchone()
    if row is not None:
        reason = kb._json_dict(row[0]).get("reason")
    if not reason:
        run = conn.execute("SELECT outcome, metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
                           "ORDER BY ended_at DESC LIMIT 1", (task_id,)).fetchone()
        if run is not None and _is_owner_restart_run(run):
            reason = "owner_restart_cooldown"
    until = guard_hold_until(conn, task_id, str(reason), int(now)) if reason else None
    return {"hold_reason": str(reason), "hold_until": int(until)} if until else {}


def _board_key(conn) -> str:
    """Identity of the board ``conn`` reads, for :data:`_LAG_WARNED`: its main DB file ("" in memory)."""
    from hermes_cli.kanban_db_connect import _main_db_file
    return _main_db_file(conn) or ""


def sweep(conn, *, now: Optional[float] = None) -> int:
    """Append the once-per-ready-episode notice for every qualifying card; returns how many were appended.

    "No worker claiming it" is a Board fact read from this gateway's live copy, so it is checked against the pulled
    replica first (``kanban_replica_freshness``, t_01a1d390d19239dd): while the live feed lags the replica past the
    bound every notice waits (the episode keeps its key, so it fires once when the lag closes, if still ready), and
    a card whose newer replica row has left this ready episode is skipped. A deferral that outlasts
    ``MAX_EPISODE_SECONDS`` drops that episode's notice for good (the stall itself is the sync loop's to report), so
    the first deferred sweep of each stall (one run of replica lag, see :data:`_LAG_WARNED`) logs at WARNING."""
    from hermes_cli import kanban_replica_freshness as fresh
    from hermes_cli.kanban_sync_diagnostics import missing_board, update_hold
    now = time.time() if now is None else now
    rows = conn.execute(_CANDIDATES, (int(now - MAX_EPISODE_SECONDS), int(now - THRESHOLD_SECONDS))).fetchall()
    due = []
    for row in rows:
        since = pu.ready_since(conn, row["id"], row["created_at"])
        ready_for = now - since
        if THRESHOLD_SECONDS <= ready_for <= MAX_EPISODE_SECONDS:
            due.append((row, since, ready_for))
    if not due:  # the stall is the lag, not the waiting card: it ends when the lag closes, even with nothing due
        if _LAG_WARNED:
            board = _board_key(conn)
            if board in _LAG_WARNED and fresh.lag_hold(conn, now=now) is None:
                _LAG_WARNED.discard(board)
        return 0
    lag = fresh.lag_hold(conn, now=now)
    board = _board_key(conn)
    if lag is not None:
        (_log.info if board in _LAG_WARNED else _log.warning)(
            "kanban chat-wait: %d notice(s) deferred: %s (board %s)", len(due), lag["reason"], board or "<memory>")
        _LAG_WARNED.add(board)
        return 0
    _LAG_WARNED.discard(board)
    newer = fresh.newer_rows(conn, [row["id"] for row, _since, _for in due])
    drain = update_hold()
    posted = 0
    for row, since, ready_for in due:
        moved = fresh.moved_past(newer.get(row["id"]))
        if moved:
            _log.info("kanban chat-wait: %s skipped: %s", row["id"], moved)
            continue
        if pu.post_notice(conn, row["id"], row["id"], wait_key=f"ready@{since}", waited_seconds=ready_for,
                          ready_seconds=ready_for, parent_title=row["title"], kind=NOTICE_KIND,
                          extra={**drain, **_clock_hold(conn, row["id"], since, now),
                                 **missing_board(conn, row["id"], since)}):
            posted += 1
    return posted


def safe_sweep(conn) -> int:
    """:func:`sweep` for the dispatcher tick: a notice failure is logged and never stops dispatch."""
    try:
        return sweep(conn)
    except Exception:  # health: allow BLE001 -- a notice failure must never stop dispatch
        _log.warning("kanban dispatcher: chat-wait notice sweep failed", exc_info=True)
        return 0
