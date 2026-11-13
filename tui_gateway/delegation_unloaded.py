"""Deliver async-delegation results to parent chats that no live poller delivers them to. [daveey]

A finished async delegation sits in the ledger (``async_delegations``, ``delivery_state='pending'``) until a TUI
poller claims its in-memory copy and starts the parent's turn. Three gaps left such a result waiting for hours
(``deleg_5facc89f``: durable at 10:06Z, delivered 18:20Z on 2026-11-11):

- the parent chat is loaded in NO backend: no poller exists to claim it until someone opens the chat;
- the chat is loaded here only headless (a kanban wake resumed it): a copy restored at process start
  (``startup_replay``) is refused by every session without a live transport
  (``session_notifications._session_owns_notification_event``), so the loaded chat still does not get it;
- the chat is held by backend B while backend A holds the copy: A's turn is refused ``SESSION_NOT_OWNED`` and
  retried with backoff, each retry claiming and deferring the row, which keeps it "recently touched", so B's orphan
  sweep (``async_delegation._copy_may_be_live``) never re-offers it; B has no copy, A cannot run the turn.

This pass runs in every Desktop/TUI backend from the kanban unloaded-session sweeper's loop. For each terminal row
past ``GRACE_S`` (a live poller delivers a fresh result within seconds), not under a live delivery claim, whose event
was dispatched from a UI chat (``origin_ui_session_id``; messaging-gateway parents are the gateway's), and whose
chat is in this host's stores, it:

- skips the chat if it is archived or moved to another gateway (``notice_fanout.chat_skip_reason``, as the kanban
  sweep; the row stays pending and unclaimed), if another backend holds its active-session lease and this one does
  not (that backend's own pass
  delivers it), or if the chat is live here mid-turn (its poller delivers at the turn's end);
- otherwise claims the row (``claim_completion_delivery``: the one atomic cross-process gate, so the poller's copy,
  another backend's pass and this one deliver at most once between them) and runs ONE turn with the result in the
  chat (``server._wake_session_turn``: the chat live here as is, else resumed here detached, as the kanban wake does);
- acknowledges the row only once the turn started. A busy/refused/failed wake defers the claim (no attempt spent)
  and backs this row off (``RETRY_S`` doubling to ``RETRY_MAX_S``); a chat no store has releases it (spends an
  attempt, so the ledger's budget ends it as ``dropped``).

A started wake spends the chat's budget (``WAKES_PER_HOUR``; a woken turn may dispatch work whose result wakes it
again). A chat past budget keeps its result pending: its next poller, or the next hour's pass, delivers it. The pass
runs (and is disabled) with the kanban unloaded-session sweeper's loop.

Under the host loop lease (``kanban_unloaded``, design t_01a1d45d75851804 §2b) only the lease holder runs the full
pass; it skips a chat another backend holds (``skipped:held``) as before. Every other backend runs the pass with
``only=`` its own live session ids, so a chat live in a backend that is not the holder still gets its result from
that backend. The budget is per process, so with one holder it is in effect host-wide for unloaded chats.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from tui_gateway.wake_slots import WakeSlots

logger = logging.getLogger(__name__)

LABEL = "deleg-sweep-wake"
GRACE_S = 90.0
WAKES_PER_HOUR = 6
MAX_PARALLEL_WAKES = 2
RETRY_S = 120.0
RETRY_MAX_S = 1800.0
RECOVER_EVERY_S = 300.0  # dead-owner classification per ledger; the pass itself ticks every 30 s

_lock = threading.Lock()
_recovered_at: dict[str, float] = {}  # ledger home -> last recover_abandoned_delegations run (this process)
_wake_times: dict[str, list[float]] = {}  # chat -> started wakes (this process)
_retry: dict[str, tuple[float, float]] = {}  # delegation id -> (not before, last delay)
_wake_slots = WakeSlots(MAX_PARALLEL_WAKES)


def _reset_for_tests() -> None:
    global _wake_slots
    with _lock:
        _wake_times.clear()
        _retry.clear()
        _recovered_at.clear()
    _wake_slots = WakeSlots(MAX_PARALLEL_WAKES)


def ledger_homes() -> list[Optional[Path]]:
    """Launch home (None: no override) first, then each named profile home with a state.db."""
    from tui_gateway import server
    homes: list[Optional[Path]] = [None]
    for profile in server._readopt_wake_profiles()[1:]:
        with contextlib.suppress(Exception):
            if (home := server._profile_home(profile)) is not None:
                homes.append(home)
    return homes


@contextlib.contextmanager
def _home_scope(home: Optional[Path]) -> Iterator[None]:
    if home is None:
        yield
        return
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def wake_turn(chat: str, text: str, metadata: dict) -> str:
    from tui_gateway import server
    return server._wake_session_turn(chat, text, display_kind="async_delegation_complete",
                                     display_metadata=metadata, label=LABEL)


def busy_here(ids: set[str]) -> bool:
    """Whether a session this process holds for one of ``ids`` is mid-turn (its own poller delivers after it)."""
    from tui_gateway import server
    with server._sessions_lock:
        for sid, session in server._sessions.items():
            if not isinstance(session, dict) or session.get("_finalized"):
                continue
            keys = {sid, str(session.get("session_key") or ""), str(getattr(session.get("agent"), "session_id", "") or "")}
            if keys & ids and session.get("running"):
                return True
    return False


def held_elsewhere(holders: Callable[[], list[dict]], ids: set[str]) -> Optional[str]:
    """``pid N`` of another live backend holding one of ``ids`` when this process holds none, else None."""
    me, other = os.getpid(), None
    for entry in holders() or ():
        if str(entry.get("session_id") or "") not in ids:
            continue
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid == me:
            return None
        other = other or f"pid {pid}"
    return other


def _spawn_thread(job: Callable[[], Any]) -> str:
    from agent.local_work_agent import scope as work_scope
    from agent.memory_provider import spawn_context_thread
    # Reserve BEFORE the thread exists: spawn_context_thread then returns a TrackedThread that keeps custody.
    with work_scope("sweep", "delegation_unloaded.spawn"):
        spawn_context_thread(job, name=LABEL).start()  # keeps the pass's profile home binding
    return "dispatched"


def _has_budget(chat: str, now: float) -> bool:
    with _lock:
        _wake_times[chat] = [t for t in _wake_times.get(chat, ()) if t > now - 3600]
        return len(_wake_times[chat]) < WAKES_PER_HOUR


def _backing_off(delegation_id: str, now: float) -> bool:
    with _lock:
        return _retry.get(delegation_id, (0.0, 0.0))[0] > now


def _back_off(delegation_id: str, now: float) -> None:
    with _lock:
        delay = min(_retry.get(delegation_id, (0.0, RETRY_S / 2))[1] * 2, RETRY_MAX_S)
        _retry[delegation_id] = (now + delay, delay)


def _wake_job(wake, delegation_id: str, claim: str, chat: str, text: str, metadata: dict, now: float) -> str:
    """Run on a worker thread: the wake, then settle the claim by its verdict. Never raises."""
    from tools import async_delegation as ad
    try:
        try:
            verdict = str(wake(chat, text, metadata) or "error")
        except Exception:
            logger.warning("%s of %s for %s failed", LABEL, chat, delegation_id, exc_info=True)
            verdict = "error"
        if verdict == "started":
            if not ad.complete_completion_delivery(delegation_id, claim):
                # The turn ran but the claim lease lapsed before the ack: another consumer may deliver it again.
                logger.warning("%s: started a turn in chat %s for async delegation %s, but its claim lease lapsed "
                               "before the ack; the row was not marked delivered", LABEL, chat, delegation_id)
            with _lock:
                _wake_times.setdefault(chat, []).append(now)
                _retry.pop(delegation_id, None)
            logger.info("%s: started a turn in chat %s for async delegation %s", LABEL, chat, delegation_id)
        else:
            (ad.release_completion_delivery if verdict == "missing" else ad.defer_completion_delivery)(
                delegation_id, claim)
            _back_off(delegation_id, time.time())
            logger.info("%s: chat %s did not take async delegation %s (%s); it stays pending", LABEL, chat,
                        delegation_id, verdict)
        return verdict
    except Exception:
        logger.warning("%s job for %s failed", LABEL, delegation_id, exc_info=True)
        with contextlib.suppress(Exception):
            ad.defer_completion_delivery(delegation_id, claim)
        return "error"
    finally:
        _wake_slots.release()


def _chat_and_tip(chat: str, opener) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(chat, tip, skip)``: ``skip`` is ``notice_fanout.chat_skip_reason`` (``archived`` / ``moved``), the gate
    the kanban sweep applies. ``session.resume`` refuses neither, so without it an archived chat would be revived
    and a chat moved to another gateway would run a turn here. (None, None, None) when no store has the chat."""
    from tui_gateway.notice_fanout import chat_skip_reason
    with opener(chat) as (db, tip):
        if db is None:
            return None, None, None
        return chat, tip, chat_skip_reason(db, chat)


def sweep_once(*, now: Optional[float] = None, grace_s: float = GRACE_S,
               homes: Callable[[], list] = ledger_homes, opener=None,
               holders: Optional[Callable[[], list[dict]]] = None,
               busy: Callable[[set], bool] = busy_here, wake: Callable[..., str] = wake_turn,
               spawn: Callable[[Callable[[], Any]], Any] = _spawn_thread,
               only: Optional[set[str]] = None) -> list[dict]:
    """One pass over every ledger this backend serves; one decision dict per candidate row (for logs and tests).
    ``only`` (a non-holder's residual pass): act only on rows whose chat or its tip is one of these live ids, and
    leave the ledger's dead-owner recovery to the holder."""
    from tools import async_delegation as ad
    from tools.process_registry_notifications import format_process_notification
    from tui_gateway import kanban_unloaded as ku
    from tui_gateway import server
    opener = opener or ku.find_session_db
    holders = holders or ku.registry_holders
    now = time.time() if now is None else now
    out: list[dict] = []
    for home in homes():
        with _home_scope(home):
            with _lock:
                key = str(home or "")
                recover = only is None and now - _recovered_at.get(key, float("-inf")) >= RECOVER_EVERY_S
                if recover:
                    _recovered_at[key] = now
            try:
                rows = ad.unclaimed_pending_completions(min_age_s=grace_s, now=now, recover=recover)
            except Exception:
                logger.debug("%s: ledger of %s unreadable", LABEL, home or "launch home", exc_info=True)
                continue
            for row in rows:
                did, evt = row["delegation_id"], row["event"]
                decision = {"delegation_id": did, "home": str(home or ""), "verdict": None}
                out.append(decision)
                chat = str(evt.get("session_key") or evt.get("parent_session_id") or "")
                if (evt.get("type") != "async_delegation" or ad.is_interim_delegation_event(evt)
                        or not evt.get("origin_ui_session_id") or not chat):
                    decision["verdict"] = "skipped:not-a-ui-result"
                    continue
                if _backing_off(did, now):
                    decision["verdict"] = "skipped:backoff"
                    continue
                found, tip, skip = _chat_and_tip(chat, opener)
                if found is None:
                    decision["verdict"] = "skipped:no-store"
                    continue
                ids = {chat, tip or chat}
                if only is not None and not ids & only:
                    decision["verdict"] = "skipped:not-live-here"  # the lease holder's pass owns it
                    continue
                if skip is not None:
                    # Left pending and unclaimed: an unarchived chat's poller can still take it, else the ledger's
                    # expiry ends it. Back off so the pass does not re-read the chat every tick.
                    _back_off(did, now)
                    decision["verdict"] = f"skipped:{skip}"
                    continue
                if (holder := held_elsewhere(holders, ids)) is not None:
                    decision["verdict"] = f"skipped:held:{holder}"
                    continue
                if busy(ids):
                    decision["verdict"] = "skipped:busy"
                    continue
                if not _has_budget(tip or chat, now):
                    decision["verdict"] = "skipped:budget"
                    continue
                text = format_process_notification(evt)
                if not text:
                    decision["verdict"] = "skipped:no-text"
                    continue
                if not _wake_slots.acquire(blocking=False):
                    decision["verdict"] = "skipped:slots"
                    continue
                claim = f"{LABEL}:{os.getpid()}:{uuid.uuid4().hex}"
                try:
                    claimed = ad.claim_completion_delivery(did, claim)
                except sqlite3.Error:
                    logger.debug("%s: claim of %s failed; the next pass retries", LABEL, did, exc_info=True)
                    claimed = False
                if not claimed:
                    _wake_slots.release()
                    decision["verdict"] = "skipped:claimed"
                    continue
                meta = {**server._async_delegation_display_metadata(evt), "woken": True}
                job = lambda did=did, claim=claim, chat=tip or chat, text=text, meta=meta: _wake_job(
                    wake, did, claim, chat, text, meta, now)
                try:
                    decision["verdict"] = str(spawn(job))
                except Exception:
                    _wake_slots.release()
                    ad.defer_completion_delivery(did, claim)
                    logger.warning("%s could not start a wake for %s", LABEL, did, exc_info=True)
                    decision["verdict"] = "error"
    return out
