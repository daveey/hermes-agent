"""Legacy Desktop ``tui`` kanban subscriptions on chats the canonical gateway owns. [daveey]

A Desktop chat subscribes to its cards as ``platform='tui'``. Only the legacy Desktop backend
(``tui_gateway.session_notifications``) claims those rows. Once the chat moves to the canonical
gateway (its turns run through the LOCAL route: one owner, one FIFO, #140385), that backend can no
longer write the notice: every claim is refused ``SESSION_NOT_OWNED`` and handed back, with a backoff
that doubles to 300 s. The gateway collector meanwhile skipped ``tui`` rows because no ``tui`` adapter
is connected. While the gateway ran turns back to back, nothing delivered: the notices waited for the
whole busy chain to end (64 min on 2026-11-11, t_01a1ce31dbbb74bd).

This module lets the gateway take exactly the rows the legacy backend must refuse. Those are rows for
a chat with a live LOCAL session in this gateway whose durable ledger holds forwardable work, or
whose turn lease this process holds. The notice rides the session FIFO as a kanban wake, like the
chat's ``local`` subscriptions (``admit_route_wake``). It never runs as a classic turn and never
restores a closed session. Every failure rewinds the claim, with no failure count and no
subscription drop, so the legacy backend can still deliver it once the chat is back there. The
cursor key stays ``tui`` throughout.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any, Optional

from gateway.kanban_watchers_common import logger

TUI = "tui"
# Ledger states that end on their own (tui_gateway.owner_forward.FORWARDABLE_LEDGER_STATES): an
# ``unknown`` admission is never resolved by waiting, so the legacy refusal stands and nothing queues behind it.
_FORWARDABLE_LEDGER_STATES = frozenset({"started", "queued", "registered", "running"})


def notify_kinds() -> tuple:
    """The kinds the legacy tui poller claims (silent kinds included, so they never wedge the cursor)."""
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS
    return _KANBAN_NOTIFY_KINDS


def deliverable_kinds():
    from tui_gateway.session_notifications import _KANBAN_EVENT_FORMATTERS
    return _KANBAN_EVENT_FORMATTERS


def format_text(sub: dict, task: Any, ev: Any, board_slug: Optional[str]) -> Optional[str]:
    """The legacy tui notice text for one event: the same words the Desktop transcript would show."""
    from tui_gateway.session_notifications import _format_kanban_event_text
    return _format_kanban_event_text(sub, task, ev, board_slug or "")


def _launch_authority(runner: Any, profile: Optional[str]) -> Any:
    """The authority that serves ``profile``'s local sessions in this process, else None.

    The collector runs in a worker thread with no profile scope bound, so ``active_authority`` would
    answer with the launch authority for every profile. A sub stamped with another served profile is
    left to the legacy backend instead of being matched against the wrong session store."""
    registry = getattr(runner, "session_authorities", None)
    authority = registry.launch if registry is not None else getattr(runner, "session_authority", None)
    if authority is None or not profile:
        return authority
    name = None
    if registry is not None:
        name = registry.profile_name(authority)
    if not name:
        name = getattr(runner, "_kanban_notifier_profile", None) or getattr(runner, "_primary_profile_name", None)
    return authority if not name or name == profile else None


def _gateway_holds_work(authority: Any, ids: set) -> bool:
    """Whether the gateway's ledger holds work that ends on its own for ``ids``, or this process holds
    the turn lease. This is when the legacy backend refuses the chat. A read error answers False and
    leaves the row to the legacy backend."""
    from tui_gateway.session_lifecycle import _CANONICAL_PENDING_SQL
    db = getattr(authority, "db", None)
    read_ctx = getattr(db, "_read_ctx", None)
    if read_ctx is None:
        return False
    try:
        with read_ctx() as conn:
            for sid in sorted(filter(None, ids)):
                row = conn.execute(_CANONICAL_PENDING_SQL, (sid, sid)).fetchone()
                if row is not None:
                    return row[0] in _FORWARDABLE_LEDGER_STATES
        owner_of = getattr(db, "session_turn_lease_owner", None)
        if owner_of is not None:
            mine = f"pid={os.getpid()}:"
            for sid in sorted(filter(None, ids)):
                owner = owner_of(sid)
                if owner and str(owner.get("holder") or "").startswith(mine):
                    return True
    except sqlite3.Error:
        logger.debug("kanban notifier: tui owner probe failed; leaving the row to the legacy backend", exc_info=True)
    return False


def owned_live_session(runner: Any, sub: dict, *, require_work: bool = True) -> Any:
    """The live LOCAL session this gateway runs for ``sub``'s chat, else None.

    A prefilter safe in the collector thread: one ``dict.get`` on the authority's sessions (never an
    iteration) and read-only SQLite, with nothing restored. ``require_work`` (collector) also
    requires the condition under which the legacy backend refuses the chat. The delivery re-check
    drops it: a turn that ended meanwhile still leaves the gateway as the chat's owner."""
    from gateway.config import Platform
    if Platform.LOCAL not in (getattr(runner, "adapters", None) or {}):
        return None
    chat = str(sub.get("chat_id") or "")
    if not chat or sub.get("thread_id"):
        return None
    authority = _launch_authority(runner, sub.get("notifier_profile") or None)
    if authority is None:
        return None
    try:
        # tui rows bind to the live compression tip; the authority keys a session by its lineage root.
        logical = authority.logical_owner(chat) or chat
    except sqlite3.Error:
        logical = chat
    sessions = getattr(authority, "sessions", None) or {}
    live = sessions.get(logical)
    if live is None and logical != chat:
        live = sessions.get(chat)
    source = getattr(live, "source", None)
    if live is None or getattr(source, "platform", None) != Platform.LOCAL:
        return None
    if getattr(live, "route", None) != runner._session_key_for_source(source):
        return None
    if require_work and not _gateway_holds_work(authority, {chat, logical}):
        return None
    return live


def has_local_sibling(conn: Any, sub: dict) -> bool:
    """The same card already notifies this chat through a ``local`` row (gateway-native): that row
    delivers it here, so serving the tui row too would wake the chat twice."""
    try:
        row = conn.execute(
            "SELECT 1 FROM kanban_notify_subs WHERE task_id = ? AND platform = 'local' AND chat_id = ? LIMIT 1",
            (sub["task_id"], sub["chat_id"]),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def claim(collector: Any, conn: Any, slug: str, sub: dict) -> Optional[dict]:
    """Claim one tui row's unseen events for this gateway, or None (left to the legacy backend)."""
    if has_local_sibling(conn, sub) or owned_live_session(collector.runner, sub) is None:
        return None
    from hermes_cli import kanban_db_notify as _kbn
    old_cursor, cursor, events = _kbn.claim_unseen_events_for_sub(
        conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
        thread_id=sub.get("thread_id") or "", kinds=notify_kinds(), deliverable_kinds=deliverable_kinds(),
        durable=True,
    )
    if not events:
        return None
    task = collector.kb.get_task(conn, sub["task_id"])
    logger.debug("kanban notifier: claimed %d tui event(s) for %s on board %s cursor %s→%s (gateway owns %s)",
                 len(events), sub["task_id"], slug, old_cursor, cursor, sub["chat_id"])
    return {"sub": sub, "old_cursor": old_cursor, "cursor": cursor, "events": events, "task": task,
            "board": slug, "tui_owner": True}


async def deliver(notification: Any) -> None:
    """Admit a claimed tui batch into the owning LOCAL session's FIFO, else rewind the claim."""
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import diagnostic_event
    sub, d = notification.sub, notification.d
    texts, diagnostic = [], True
    for ev in d["events"]:
        text = format_text(sub, notification.task, ev, notification.board_slug)
        if text:
            texts.append(text)
            diagnostic = diagnostic and diagnostic_event(ev)
    if not texts:  # silent kinds only (archived/unblocked): move the cursor past them, as the legacy poller does
        await notification.advance()
        return
    runner = notification.runner
    live = await asyncio.to_thread(owned_live_session, runner, sub, require_work=False)
    adapter = (getattr(runner, "adapters", None) or {}).get(Platform.LOCAL)
    if live is None or adapter is None:
        logger.info("kanban notifier: %s no longer runs on this gateway; handing tui notice(s) for %s back",
                    sub["chat_id"], notification.task_id)
        await notification.rewind()
        return
    from gateway.session_automation import admit_route_wake
    from hermes_state_runtime import RuntimeStoreError
    metadata = {"notification_category": "diagnostic"} if diagnostic else {}
    try:
        admitted = await admit_route_wake(runner, adapter, live.source, "\n".join(texts), "kanban-wake", metadata)
    except Exception as exc:  # health: allow BLE001 -- delivery boundary: any admission failure rewinds the claim (logged with traceback) so no notice is lost
        logger.warning("kanban notifier: local FIFO refused tui notice(s) for %s in %s (%s); rewound for retry",
                       notification.task_id, sub["chat_id"], exc, exc_info=not isinstance(exc, RuntimeStoreError))
        await notification.rewind()
        return
    if not admitted:
        logger.info("kanban notifier: no live local route for %s; tui notice(s) for %s handed back",
                    sub["chat_id"], notification.task_id)
        await notification.rewind()
        return
    await notification.advance()
    logger.info("kanban notifier: admitted %d tui notice(s) for %s to the local FIFO of %s (cursor %s→%s)",
                len(texts), notification.task_id, sub["chat_id"], d.get("old_cursor"), d.get("cursor"))
