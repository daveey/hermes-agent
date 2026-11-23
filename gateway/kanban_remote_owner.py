"""Remote Kanban notices for busy chats the canonical gateway owns ride its FIFO. [daveey]

A card placed on another gateway notifies its chat here through the hermes-hub replica
(``tui_gateway.kanban_remote_notices``). Only the Desktop backend's sweep delivered it: it writes
the passive row or wakes the chat, and a chat mid-turn refuses both (``SessionTurnLeaseLostError``),
so the claim went back for the next pass. A chat the gateway runs turn after turn (its FIFO starts
the next queued local notice within seconds; the sweep runs every 30-120 s) was never idle at a
pass: on 2026-11-12 four done notices reached chat 20261026_235722_bef6c6 3-63 min late, one 45
passes after it was first seen (t_01a1cff0541bf30c). PR #1250 closed the same gap for local-board
``tui`` rows (``gateway.kanban_tui_owner``); this is its remote leg.

Each pass, for every live LOCAL session of this gateway whose ledger holds work or whose turn lease
this process holds (``kanban_tui_owner.owned_live_session``'s test), the chat's pending remote
notices are claimed with ``kanban_remote_notices.collect`` and admitted in arrival order to its FIFO
(``admit_route_wake``, producer ``kanban-wake``) as a ``kanban_notice`` card carrying the notice's
``kanban_event_keys``. The turn's user row carries them, so custody matches the sweep's:

- the ledger claim stays in flight (this process's pid + start) until a row with its key is in the
  transcript (``notice_in_transcript``), then it is acked;
- a refused admission releases the claim, and the notice is offered again next pass;
- an admission that ended without the row (cancelled, failed) is released and offered again;
- a successor after a restart finds a dead holder's claim: acked when the row is there, adopted
  without a second admission while the FIFO still holds its key (``kanban_remote_fifo``), else
  offered again.

The sweep stands aside while a live gateway admission holds the notice's key
(``kanban_remote_fifo.gateway_holds``), so the two never deliver it twice; a dropped admission hands it back. Idle owned chats and chats with no gateway owner keep the sweep's path.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

INTERVAL_S = 10.0
SLOW_PASS_S = 5.0  # a pass longer than this logs its collect vs admit split (WARNING)
PRODUCER = "kanban-wake"
_DISPLAY_LIMIT = 2000  # session_automation._DISPLAY_TEXT_LIMIT

_lock = threading.Lock()
# (ledger session, ref, state) -> {"claim", "key", "ids", "tip"} for claims this process admitted or adopted.
_admitted: dict[tuple, dict] = {}


def _reset_for_tests() -> None:
    with _lock:
        _admitted.clear()


def _claim_id(claim: dict) -> tuple:
    return (claim["session"], claim["ref"], claim["state"])


def _children(item: dict) -> list[dict]:
    return list(item.get("children") or [item])


def _profile(runner: Any, authority: Any) -> str:
    registry = getattr(runner, "session_authorities", None)
    name = registry.profile_name(authority) if registry is not None else None
    return (name or getattr(runner, "_kanban_notifier_profile", None)
            or getattr(runner, "_primary_profile_name", None) or "default")


def _owned_lives(runner: Any, authority: Any) -> list[tuple[str, Any]]:
    """``(logical id, live session)`` of this gateway's live LOCAL chats. Runs on the event loop, the
    authority's thread (``kanban_tui_owner`` never iterates the sessions from a worker thread)."""
    from gateway.config import Platform
    out = []
    for logical, live in list((getattr(authority, "sessions", None) or {}).items()):
        source = getattr(live, "source", None)
        if getattr(source, "platform", None) != Platform.LOCAL or getattr(source, "thread_id", None):
            continue
        if getattr(live, "route", None) != runner._session_key_for_source(source):
            continue
        out.append((str(logical), live))
    return out


def _tip(authority: Any, live: Any) -> Optional[str]:
    try:
        from gateway.session_detached import route_entry
        entry = route_entry(authority, live.route)
    except Exception:  # health: allow BLE001 -- a lookup failure falls back to the chat id; never costs the pass
        logger.debug("remote kanban owner: route entry lookup failed", exc_info=True)
        return None
    return getattr(entry, "session_id", None) if entry is not None else None


def _track(claim: dict, ids: frozenset, tip: str) -> None:
    with _lock:
        _admitted[_claim_id(claim)] = {"claim": claim, "key": claim.get("event_key") or claim.get("notice_key"),
                                       "ids": ids, "tip": tip}


def _release_claim(claim: dict) -> bool:
    """Atomically release our custody, never a successor's replacement claim."""
    from tui_gateway import kanban_remote_notices as krn
    pid, start = krn._my_holder()
    try:
        with contextlib.closing(krn._ledger(None)) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM notices WHERE session=? AND ref=? AND state=? AND delivered=0 "
                             "AND holder_pid=? AND holder_start IS ?", (*_claim_id(claim), pid, start))
                row = conn.execute("SELECT delivered,holder_pid,holder_start FROM notices "
                                   "WHERE session=? AND ref=? AND state=?", _claim_id(claim)).fetchone()
                settled = row is None or bool(row[0]) or (row[1], row[2]) != (pid, start)
            return settled  # only after the commit, while replacement cannot change the proof
    except (sqlite3.Error, OSError):
        logger.warning("remote kanban release settlement unreadable", exc_info=True)
        return False


def _release_tracked(claims: list[dict], ids: frozenset, tip: str) -> None:
    for claim in claims:
        _track(claim, ids, tip)
        if _release_claim(claim):
            with _lock:
                _admitted.pop(_claim_id(claim), None)


def _reconcile(db: Any) -> None:
    """Settle claims this process admitted: ack once the row is in the transcript, keep while the FIFO holds
    the key, else release (the admission was dropped without the row). The FIFO is read BEFORE the
    transcript, so a row written between the two reads is seen, never released."""
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway.kanban_remote_fifo import pending_keys
    with _lock:
        tracked = list(_admitted.items())
    pending: dict[frozenset, set] = {}
    for claim_id, entry in tracked:
        ids, key, claim = entry["ids"], entry["key"], entry["claim"]
        if ids not in pending:
            pending[ids] = pending_keys(db, ids)
        in_fifo = key in pending[ids]
        if krn.notice_in_transcript(db, entry["tip"], key, ""):
            if not krn.ack(claim):
                continue
            logger.info("remote Kanban notice %s for chat %s is in the transcript; acked", claim["ref"],
                        claim["session"])
        elif in_fifo:
            continue
        else:  # dropped undelivered (cancelled, failed, refused): never acked, offered again
            if not _release_claim(claim):
                continue
            logger.info("remote Kanban notice %s for chat %s: its admission ended without the row; offered again",
                        claim["ref"], claim["session"])
        with _lock:
            _admitted.pop(claim_id, None)


def _adopt_pending(item: dict, pending: set, ids: frozenset, tip: str) -> Optional[dict]:
    """``item`` minus members the FIFO already holds (a predecessor admitted them before it died): those
    are adopted (tracked, no second admission). A partly held group releases the rest for the next pass."""
    held = [child for child in _children(item) if child["event_key"] in pending]
    if not held:
        return item
    for child in held:
        _track(child, ids, tip)
        logger.info("remote Kanban notice %s for chat %s is already in the gateway FIFO; adopted",
                    child["ref"], child["session"])
    rest = [child for child in _children(item) if child["event_key"] not in pending]
    if rest:
        _release_tracked(rest, ids, tip)
    return None


def _drop_written(item: dict, has: Any, ids: frozenset, tip: str) -> Optional[dict]:
    """``item`` minus members whose row is already in the chat's lineage (written by any path, under any
    ledger key): those are acked, never admitted again. A partly written group releases the rest for the
    next pass, which offers them on their own."""
    from tui_gateway import kanban_remote_notices as krn
    written = [child for child in _children(item) if has(child["event_key"], "")]
    if not written:
        return item
    for child in written:
        _track(child, ids, tip)
        if not krn.ack(child):
            continue
        with _lock:
            _admitted.pop(_claim_id(child), None)
        logger.info("remote Kanban notice %s for chat %s is already in the transcript; acked, not admitted",
                    child["ref"], child["session"])
    rest = [child for child in _children(item) if child not in written]
    if rest:
        _release_tracked(rest, ids, tip)
    return None


def _collect(runner: Any, authority: Any, lives: list, report=None) -> list[tuple]:
    """``[(live, ids, tip, items)]``: claimed notices of busy owned chats, in arrival order. Worker thread."""
    from gateway.kanban_tui_owner import _gateway_holds_work
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway.kanban_remote_fifo import lineage_ids, pending_keys
    from gateway.kanban_notification_schedule import PassReport
    report = report if report is not None else PassReport()
    db = getattr(authority, "db", None)
    _reconcile(db)
    if _admitted:
        report.retry()
    if not krn.hub_db_path().is_file():
        report.retry()
        return []
    from tui_gateway.kanban_remote_index import ensure_holder
    ensure_holder("gateway")  # outranks Desktop backends; upgrades a 'serve' refresher collect started here first
    profile = _profile(runner, authority)
    out = []
    for logical, live in lives:
        chat = str(live.source.chat_id)
        if not _gateway_holds_work(authority, {chat, logical}):
            continue  # idle: the Desktop backend's sweep wakes it or writes the row
        tip = _tip(authority, live) or chat
        ids = frozenset(lineage_ids(db, chat, logical, tip))
        pending = pending_keys(db, ids)

        def has(key: str, task_id: str, _tip: str = tip) -> bool:
            return krn.notice_in_transcript(db, _tip, key, task_id)

        items = []
        try:
            # Each notice is claimed under its link's own session (``per_link``), the ledger key the sweep
            # claims it under: on a chat compressed before the card was linked, a claim under the root id
            # was invisible to the sweep's and the notice landed twice (review t_01a1d013e0411032).
            items = krn.collect(chat, profile, lineage=tuple(sorted(ids - {chat})), transcript_has=has,
                                per_link=True)
            if not getattr(items, "quiet", False):
                report.retry()
            elif getattr(items, "next_deadline", None) is not None:
                report.due_at(items.next_deadline)
            items = [kept for item in items if (kept := _drop_written(item, has, ids, tip)) is not None]
            items = [kept for item in items if (kept := _adopt_pending(item, pending, ids, tip)) is not None]
        except Exception:  # health: allow BLE001 -- claims taken before the error go back, so none waits for an exit
            report.retry()
            logger.warning("remote kanban owner: collecting chat %s failed; releasing its claims", chat, exc_info=True)
            _release_tracked([c for item in items for c in _children(item) if _claim_id(c) not in _admitted], ids, tip)
            continue
        if items:
            out.append((live, ids, tip, items))
    return out


_CARD_TEXT_LIMIT = 32000  # kanban_wake_fold._MAX_CARD_TEXT


def _metadata(item: dict) -> dict:
    """The admission's card. ``kanban_cards`` makes it a Kanban wake that folds with the local wakes
    waiting at the FIFO tail (``kanban_wake_fold``), so a burst of notices during one turn is one turn;
    a late group is one opaque card (no task id: never merged with another card's event)."""
    text = item["text"]
    shown = text if len(text) <= _DISPLAY_LIMIT else text[:_DISPLAY_LIMIT - 1] + "…"
    keys = [child["event_key"] for child in _children(item)]
    task_id = "" if item.get("children") else str(item.get("ref") or "").rsplit("/", 1)[-1]
    parts = str(item.get("ref") or "").split("/")
    board = parts[2] if task_id and len(parts) == 4 else ""
    kind = {"blocked": "blocked", "done": "completed", "request_withdrawn": "request_withdrawn",
            "decision_requested": "decision_requested"}.get(
        str(item.get("state") or "").split("@", 1)[0])
    card = {"task_id": task_id, "board": board, "kinds": [kind] if task_id and kind else [],
            "text": text[:_CARD_TEXT_LIMIT]}
    return {"display_kind": "kanban_notice", "display_text": shown, "kanban_event_keys": keys,
            "kanban_cards": [card]}


async def _admit(runner: Any, adapter: Any, live: Any, ids: frozenset, tip: str, item: dict) -> bool:
    from gateway.session_automation import admit_route_wake
    from hermes_state_runtime import RuntimeStoreError
    chat = live.source.chat_id
    try:
        metadata = _metadata(item)
        # The turn text is the card's text, so a later fold splits it back into exactly this card.
        admitted = await admit_route_wake(runner, adapter, live.source, metadata["kanban_cards"][0]["text"],
                                          PRODUCER, metadata)
    except Exception as exc:  # health: allow BLE001 -- delivery boundary: any refusal releases the claim (logged) so no notice is lost
        logger.warning("remote Kanban notice %s for chat %s refused by the local FIFO (%s); released for retry",
                       item["ref"], chat, exc, exc_info=not isinstance(exc, RuntimeStoreError))
        admitted = False
    if not admitted:
        await asyncio.to_thread(_release_tracked, _children(item), ids, tip)
        logger.info("remote Kanban notice %s for chat %s not admitted; released for the next pass", item["ref"], chat)
        return False
    for child in _children(item):
        _track(child, ids, tip)
    logger.info("remote Kanban notice %s for chat %s admitted to the local FIFO (%d key(s)); claim in flight "
                "until its row is written", item["ref"], chat, len(_children(item)))
    return True


async def tick(runner: Any, *, report=None) -> list[dict]:
    """One pass; the admitted notices. Never raises. The caller sets the cadence (the gateway's remote owner
    watcher, every ``INTERVAL_S`` by default)."""
    from gateway.kanban_notification_schedule import PassReport
    report = report if report is not None else PassReport()
    try:
        from gateway.config import Platform
        from gateway.kanban_tui_owner import _launch_authority
        adapter = (getattr(runner, "adapters", None) or {}).get(Platform.LOCAL)
        authority = _launch_authority(runner, None)
        if adapter is None or authority is None:
            report.retry()
            return []
        started = time.monotonic()
        plan = await asyncio.to_thread(_collect, runner, authority, _owned_lives(runner, authority), report)
        collected = time.monotonic()
        out = []
        for live, ids, tip, items in plan:
            for item in items:
                if await _admit(runner, adapter, live, ids, tip, item):
                    out.append(item)
                else:
                    report.retry()
        if _admitted:
            report.retry()
        done = time.monotonic()
        if done - started > SLOW_PASS_S:  # the pass's own cost, separate from the FIFO's: t_01a1d4de28a8510d
            logger.warning("remote kanban owner pass took %.1fs (collect %.1fs over %d chat(s), admit %.1fs for "
                           "%d notice(s))", done - started, collected - started, len(plan), done - collected,
                           sum(len(items) for *_, items in plan))
        return out
    except Exception:  # health: allow BLE001 -- a failed pass never stops the remote owner watcher; the next pass retries
        report.retry()
        logger.warning("remote kanban owner pass failed", exc_info=True)
        return []
