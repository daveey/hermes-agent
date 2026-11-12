"""One Kanban event, one turn, when a chat holds both a ``tui`` and a waking ``local`` row for a card. [daveey]

A Desktop chat on the canonical gateway subscribes to its cards twice: a ``local`` row with
``delivery_mode='notify+wake'`` (the gateway notifier wakes the chat; the LOCAL adapter's text ping is a
no-op) and a ``tui`` ``notify`` row (the legacy Desktop poller and the unloaded sweeper write a ``kanban_notice``
row and start a turn). Each row has its own cursor, so one completion started two model turns minutes apart
(t_01a1ce9070eee14c: messages 872084 and 872153 for t_01a1ce83e793e24f).

The waking ``local`` row owns the turn for every event it wakes on, and the wake turn carries the notice line the
``tui`` row would have shown (:func:`notice_lines`). The ``tui`` consumers stay silent only for events that row has
already delivered (:func:`tui_cover`); while it still owes a covered event they leave the ``tui`` row unclaimed
(:func:`tui_defer`), so a wake that never lands (the row dropped after repeated failures, no LOCAL adapter, past
:data:`DEFER_MAX_S`, a window counted from the later of the event and the lift of a core-drain hold that kept the
wake waiting) still leaves the ``tui`` notice owed and delivered: a duplicate beats a lost notice. Kinds the wake leg
does not wake on (``status``, quota and re-pin notices) stay with the ``tui`` row. A ``notify``-only ``local`` row
covers nothing.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

WAKE_MODES = ("notify+wake", "wake")
# How long a ``tui`` row waits for its waking ``local`` sibling to deliver a covered event before delivering it
# itself. A healthy wake acks at admission to the chat's FIFO, seconds after the event.
DEFER_MAX_S = 600.0


def _sibling(conn: Any, sub: dict, platform: str, modes: Optional[tuple] = None) -> bool:
    sql = ("SELECT 1 FROM kanban_notify_subs WHERE task_id = ? AND platform = ? AND chat_id = ? "
           "AND thread_id = ?")
    args: list = [sub["task_id"], platform, sub["chat_id"], sub.get("thread_id") or ""]
    if modes:
        sql += f" AND delivery_mode IN ({','.join('?' * len(modes))})"
        args.extend(modes)
    try:
        return conn.execute(sql + " LIMIT 1", args).fetchone() is not None
    except sqlite3.Error:
        return False  # unreadable: deliver as before (a duplicate beats a lost notice)


def covered(ev: Any) -> bool:
    """The waking ``local`` leg starts a turn for this event (``_WAKE_KINDS``, not a provisional failure)."""
    from gateway.kanban_watchers_notifier import _WAKE_KINDS
    from hermes_cli.kanban_failure_notice import provisional_failure
    return getattr(ev, "kind", "") in _WAKE_KINDS and not provisional_failure(ev)


def _local_acked(conn: Any, sub: dict) -> Optional[int]:
    """The event id up to which the waking ``local`` sibling of this ``tui`` row has delivered (its cursor, or the
    start of its in-flight claim), or None when there is no such row (or it is unreadable)."""
    if (sub.get("platform") or "").lower() != "tui":
        return None
    try:
        row = conn.execute(
            "SELECT last_event_id, inflight_from FROM kanban_notify_subs WHERE task_id = ? AND platform = 'local' "
            f"AND chat_id = ? AND thread_id = ? AND delivery_mode IN ({','.join('?' * len(WAKE_MODES))}) "
            "ORDER BY last_event_id LIMIT 1",
            (sub["task_id"], sub["chat_id"], sub.get("thread_id") or "", *WAKE_MODES)).fetchone()
    except sqlite3.Error:
        return None  # unreadable: deliver as before (a duplicate beats a lost notice)
    if row is None:
        return None
    return int((row[0] if row[1] is None else row[1]) or 0)


def tui_cover(conn: Any, sub: dict) -> Optional[Callable[[Any], bool]]:
    """For a ``tui`` row: a predicate for the events its waking ``local`` sibling has already delivered a turn for
    (:func:`covered` and at or below that row's acknowledged cursor), else None (deliver everything as before).

    Silence must follow the wake's delivery, not the row's existence: silencing on existence acked the ``tui`` row
    past an event the wake still owed, and a wake that then never landed lost the event (review of PR #1465)."""
    acked = _local_acked(conn, sub)
    if acked is None:
        return None

    def delivered(ev: Any) -> bool:
        return covered(ev) and int(getattr(ev, "id", 0) or 0) <= acked
    return delivered


def tui_defer(conn: Any, sub: dict, now: Optional[float] = None) -> bool:
    """True when this ``tui`` row should stay unclaimed this pass: its waking ``local`` sibling still owes a
    covered event past both cursors, younger than :data:`DEFER_MAX_S`. The wake then carries its notice and
    settles the row (:func:`settle_tui_sibling`); if the wake never lands, the row delivers it after the window or
    once the ``local`` row is gone."""
    try:
        row = conn.execute("SELECT last_event_id, inflight_from FROM kanban_notify_subs WHERE task_id = ? "
                           "AND platform = 'tui' AND chat_id = ? AND thread_id = ?",
                           (sub["task_id"], sub["chat_id"], sub.get("thread_id") or "")).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row[1] is not None:
        return False  # gone, or a claim is already in flight: the claim path owns it
    return wake_owes_after(conn, sub, int(row[0] or 0), now)


def wake_owes_after(conn: Any, sub: dict, floor: int, now: Optional[float] = None) -> bool:
    """True when this ``tui`` row's waking ``local`` sibling still owes a covered event past ``floor`` (and past
    its own cursor), younger than :data:`DEFER_MAX_S`. The window runs from the later of the event and the lift of
    the core-drain hold that keeps the wake leg waiting (:func:`_drain_held`): any age counts while the hold is on
    or was seen on less than :data:`DEFER_MAX_S` ago.

    The legacy poller also asks this right after its claim, with ``floor`` = the claim's start: a covered event
    appended between :func:`tui_defer` and the claim would otherwise ride both the ``tui`` batch and the wake turn
    (review of PR #1465 at 8be3211785), so the poller hands that claim back instead."""
    import json
    import time
    from types import SimpleNamespace

    acked = _local_acked(conn, sub)
    if acked is None:
        return False
    # While a core-update drain hold is on, the wake leg rewinds every batch and delivers it once the hold lifts,
    # so the window runs from the lift: a hold past DEFER_MAX_S let the tui row show its notice turn and the wake
    # start a second one after the hold (review of PR #1465 at 9268465a67), and a window that only stopped while
    # held let the first tui tick after the lift do the same (review of PR #1652 at 76133d4520). [daveey]
    now = now if now is not None else time.time()
    since = float("-inf") if _drain_held() or _lifted_within(now) else now - DEFER_MAX_S
    try:
        pending = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
                               "AND created_at > ?", (sub["task_id"], max(int(floor), acked), since)).fetchall()
    except sqlite3.Error:
        return False
    for kind, payload in pending:
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {}
        if covered(SimpleNamespace(kind=kind, payload=data)):
            return True
    return False


def _drain_held() -> bool:
    """The wake leg's core-drain hold (:func:`gateway.session_notifier_drain.held`); an unreadable hold marker counts
    as not held (deliver as before: a duplicate beats a lost notice)."""
    from gateway import session_notifier_drain
    try:
        return bool(session_notifier_drain.held_noted())
    except OSError:
        return False


def _lifted_within(now: float) -> bool:
    """A notifier leg saw the core-drain hold on less than :data:`DEFER_MAX_S` before ``now``
    (:func:`gateway.session_notifier_drain.last_held_at`). A stamp from the future (clock step) does not count."""
    from gateway import session_notifier_drain
    seen = session_notifier_drain.last_held_at()
    return seen is not None and 0 <= now - seen < DEFER_MAX_S


def tui_formatter(conn: Any, sub: dict, fmt: Callable) -> Callable:
    """``fmt`` (``(sub, task, ev, board) -> text | None``) with the events :func:`tui_cover` covers silenced."""
    cover = tui_cover(conn, sub)
    if cover is None:
        return fmt

    def silenced(s, task, ev, board):
        return None if cover(ev) else fmt(s, task, ev, board)
    return silenced


def has_tui_sibling(conn: Any, sub: dict) -> bool:
    """For a waking ``local`` row: a ``tui`` row for the same card and chat defers its notice to this wake."""
    return _waking_local(sub) and _sibling(conn, sub, "tui")


def tui_sibling_floor(conn: Any, sub: dict) -> Optional[int]:
    """For a waking ``local`` row: the event id up to which its ``tui`` sibling has delivered or holds a claim
    (``inflight_from`` while a claim is in flight, else ``last_event_id``), or None when there is no such row.
    The wake turn carries the ``tui`` notices for its batch's events past this floor (:func:`notice_lines`)."""
    if not _waking_local(sub):
        return None
    try:
        row = conn.execute("SELECT last_event_id, inflight_from FROM kanban_notify_subs WHERE task_id = ? "
                           "AND platform = 'tui' AND chat_id = ? AND thread_id = ?",
                           (sub["task_id"], sub["chat_id"], sub.get("thread_id") or "")).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int((row[0] if row[1] is None else row[1]) or 0)


def _waking_local(sub: dict) -> bool:
    return (sub.get("platform") or "").lower() == "local" and (sub.get("delivery_mode") or "notify") in WAKE_MODES


def carried_ids(events, floor: Optional[int]) -> frozenset:
    """The ids of this wake batch's events the ``tui`` row (at ``floor``) still owes: the wake turn carries their
    notice lines (:func:`notice_lines`), so :func:`settle_tui_sibling` may settle the ``tui`` row past them."""
    if floor is None:
        return frozenset()
    return frozenset(i for ev in events if (i := int(getattr(ev, "id", 0) or 0)) > floor)


def notice_lines(sub: dict, task: Any, events, board_slug: Optional[str], floor: Optional[int] = None) -> list[str]:
    """The notice text the deferring ``tui`` row would have written, for the wake turn: every covered event, and
    every other event of the batch the ``tui`` row still owes (past ``floor``), e.g. a ``status``, quota or re-pin
    notice ahead of the completion. Carrying only the covered ones left the ``tui`` row unsettled behind the
    uncovered one, and after an archive its batch delivered the completion again (review of PR #1465 at
    8be3211785)."""
    from tui_gateway.session_notifications import _format_kanban_event_text
    owed = carried_ids(events, floor)
    lines = []
    for ev in events:
        if not (covered(ev) or int(getattr(ev, "id", 0) or 0) in owed):
            continue
        if text := _format_kanban_event_text(sub, task, ev, board_slug or ""):
            lines.append(text)
    return lines


def settle_tui_sibling(board: Optional[str], sub: dict, upto: int, carried: frozenset = frozenset()) -> bool:
    """After this waking ``local`` row delivered its batch up to event ``upto``: record that on the ``tui`` sibling
    too, when everything the ``tui`` row would show up to there is covered. True when the row changed.

    The silence in :func:`tui_cover` needs the ``local`` row to still exist when the ``tui`` row is read. An
    archived card drains and removes the ``local`` row; a ``tui`` claim rewound after that (held 15 min, then
    "rewound to event 239597 for redelivery") found no sibling and delivered the completion as a second turn
    (t_01a1cfb6db097701: rows 879522 and 880235, 2026-11-12). So:

    - no claim on the ``tui`` row: its cursor moves to ``upto``;
    - a claim in flight: its rewind point (``inflight_from``) moves up to ``upto`` (never past the claim's end),
      so a rewind hands back only what the wake did not carry; the holder's own ack is unchanged.

    An event the ``tui`` row would show but the wake does not wake on settles only when its id is in ``carried``
    (the wake turn showed its line, :func:`carried_ids`); any other such event keeps the row where it is.
    """
    import contextlib
    import json
    from types import SimpleNamespace

    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_failure_notice import provisional_failure
    from tui_gateway.session_notifications import _KANBAN_EVENT_FORMATTERS
    if not _waking_local(sub):
        return False
    ident = (sub["task_id"], sub["chat_id"], sub.get("thread_id") or "")
    key = "task_id = ? AND platform = 'tui' AND chat_id = ? AND thread_id = ?"
    with contextlib.closing(kbc.connect(board=board)) as conn, kbc.write_txn(conn):
        row = conn.execute(f"SELECT last_event_id, inflight_from FROM kanban_notify_subs WHERE {key}", ident).fetchone()
        if row is None:
            return False
        last, held = int(row[0] or 0), row[1]
        start = last if held is None else int(held)
        stop = upto if held is None else min(upto, last)
        if start >= stop:
            return False
        for ev_id, kind, payload in conn.execute("SELECT id, kind, payload FROM task_events WHERE task_id = ? "
                                                 "AND id > ? AND id <= ?", (ident[0], start, stop)):
            try:
                data = json.loads(payload) if payload else {}
            except ValueError:
                data = {}
            ev = SimpleNamespace(kind=kind, payload=data)
            if provisional_failure(ev):
                continue  # the tui row shows nothing for it; its failure_finalized carries the notice
            if kind in _KANBAN_EVENT_FORMATTERS and not covered(ev) and int(ev_id) not in carried:
                return False  # the tui row still owes a notice the wake did not carry
        if held is None:
            cur = conn.execute(f"UPDATE kanban_notify_subs SET last_event_id = ? WHERE {key} "
                               "AND last_event_id = ? AND inflight_from IS NULL", (stop, *ident, last))
        else:
            cur = conn.execute(f"UPDATE kanban_notify_subs SET inflight_from = ? WHERE {key} "
                               "AND inflight_from = ?", (stop, *ident, held))
        return cur.rowcount == 1
