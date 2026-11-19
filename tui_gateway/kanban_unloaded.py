"""Deliver kanban results to chats that are not loaded in any backend. [daveey]

``session_notifications._kb_poll_board`` claims ``platform="tui"`` subscriptions only for sessions held in memory
("one poller per live session"). A chat whose backend died (websocket drop, parent-death watchdog,
``startup_orphan_reap``) kept its subscriptions, but nothing claimed their events until someone typed into it: four
cards that finished 10-16/10-17 reached session 68bf46 on 10-21 (t_15e965e8).

This sweeper runs in every gateway process. Each pass it looks for ``tui`` subscriptions whose chat this process does
not hold and whose oldest unseen terminal event has waited at least ``grace`` seconds (a live poller anywhere,
including another backend, claims within ~5 s, so it always wins that race). It then claims the range with the same
atomic cursor CAS the live poller uses, so a range is claimed by exactly one side, appends the notice to the chat's
transcript (``display_kind="kanban_notice"``, deduped by range in the writer txn) and marks the row unread. A refused
or failed append rewinds the claim, so the notice is retried instead of lost.

A row is not a turn: on 2026-10-27, 19 chats sat on such rows until David opened them, 5 of them held by a backend
whose live poller never claimed the event. So the sweeper first tries ONE turn with the notice
(``server._wake_session_turn``: resumes the chat here if no backend holds it, refuses if another ATTACHED backend owns
it, takes over a detached owner's lease) on a worker thread (at most ``MAX_PARALLEL_WAKES`` at once, one per chat; an
agent build may take minutes and must not stall the pass), and writes the passive row only when that can't start, when the card is blocked on a human (the chat is already red),
when the notice is diagnostic-only, when the chat's wake budget is spent (``HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR``,
default 4: a woken turn can create a card whose notice wakes it again) or when ``HERMES_KANBAN_UNLOADED_WAKE=0``.
A chat another live backend holds reaching the sweeper means that backend's poller missed: it is logged with the
holder ("poller miss"). Claimed events always end as a turn or a row: any error after the claim falls back to the row,
and a failed row rewinds the claim. The claim is durable (holder pid + start) until that row or the started
turn is acked; a sweeper that dies in between is rewound by ``recover_orphaned_notify_claims`` on the next
pass, the same guarantee as the live poller. A headless wake stays live only for the detached-turn grace
(default 60 s) after settlement, so nearby notices reuse its poller; after cleanup later notices return here.
Protected background work keeps its poller but releases the idle writer lease. Residual: if submit raises after the turn thread already started, the row is written too.
A chat mid-turn refuses the row (``SessionTurnLeaseLostError``): that is "busy, try later", so the claim is rewound
with one INFO line and the next pass retries; a notice still stalled after ``OVERDUE_S`` logs one WARNING per card
and chat with the reason (kanban-e5babca565).

Host loop lease (design t_01a1d45d75851804 §2b). Every gateway, Desktop ``serve`` and dashboard process starts this
loop, and the sweeps are host-wide work (the cursor CAS made N racers exactly-once, at N times the CPU). The loop now
runs its HOST pass (remote and local sweeps, delegation and guarantee for every chat, the card-change fan-out, the
killed-subagent retry over every profile store) only while it holds the ``kanban-unloaded`` lease
(``hermes_cli.host_loop_lease``: gateway > serve > dashboard, ``lease.beat()`` after each pass). Every other process
runs a RESIDUAL pass for the chats it holds itself (``live_session_ids()``): the delegation and guarantee passes
skip a chat another backend holds (``skipped:held``), so those deliveries must come from the holding process. The
holder starts up to ``wake_slots.HOLDER_SLOTS`` (8) wakes per pass kind instead of 2; when they are all busy, the
claim is rewound and the next pass retries (``deferred:slots``) instead of writing a passive row, until the notice
is ``OVERDUE_S`` old (then the row is written, as before). The per-chat wake budget
(``HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR``) is counted by the holder, so it is now in effect host-wide; a hand-off
starts it afresh. ``HERMES_HOST_LOOP_LEASE=0`` restores the per-process passes (every process its own host pass,
with its base wake slots and the killed-subagent retry over its own store only); a lease that failed open does the
same, since ``held()`` is then True in every process (``lease.exclusive()`` is False).
[daveey]
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from tui_gateway import wake_slots
from hermes_cli import loop_accounting as _la

logger = logging.getLogger(__name__)

GRACE_S = 30.0
INTERVAL_S = 60.0  # [daveey] t_01a1dfa94f2251cd: halves fixed per-pass host_pass CPU (David-delegated cadence change)
FIRST_RUN_DELAY_S = 20.0
LEASE_NAME = "kanban-unloaded"

WAKES_PER_HOUR = 4
MAX_PARALLEL_WAKES = 2
SLOTS_DEFERRED = "deferred:slots"

_sweeper_started = False
_sweeper_lock = threading.Lock()
_wake_lock = threading.Lock()
_wake_times: dict[str, list[float]] = {}  # chat -> recent started wakes (this process)
_ask_exempt: dict[tuple[str, str], float] = {}  # (chat, card) -> last budget-exempt ask wake (this process)
ASK_EXEMPT_S = 3600.0
_wake_slots = wake_slots.WakeSlots(MAX_PARALLEL_WAKES)
_slot_waits: dict[tuple, float] = {}  # remote notice -> when slots first refused it (this process)

# A chat mid-turn refuses notice rows (SessionTurnLeaseLostError): a normal "busy, try later", not a failure.
# Each claim that had to be rewound records why; a notice still waiting OVERDUE_S after its event logs ONE warning
# per (board, card, chat) with that reason, instead of a traceback every pass (kanban-e5babca565: 277 in 2h20m).
OVERDUE_S = 900.0
_stall_lock = threading.Lock()
_stalls: dict[tuple[str, str, str], str] = {}  # (board, task, chat) -> why the last claim was rewound
_overdue_warned: set[tuple[str, str, str]] = set()


def _reset_wake_budget_for_tests() -> None:
    with _wake_lock:
        _wake_times.clear()
        _ask_exempt.clear()


def _reset_overdue_for_tests() -> None:
    with _stall_lock:
        _stalls.clear()
        _overdue_warned.clear()


def _stall_key(slug: str, ident: dict) -> tuple[str, str, str]:
    return (slug, ident["task_id"], ident["chat_id"])


def _note_stall(key: tuple[str, str, str], reason: str) -> bool:
    """Record why a claim was rewound; True the first time this notice stalls (log it at INFO once)."""
    with _stall_lock:
        first = key not in _stalls
        _stalls[key] = reason
        return first


def _clear_stall(key: tuple[str, str, str]) -> None:
    with _stall_lock:
        _stalls.pop(key, None)
        _overdue_warned.discard(key)


def _clear_drained_stalls(slug: str, subs: list[dict]) -> None:
    active = {_stall_key(slug, sub) for sub in subs}
    with _stall_lock:
        for key in list(_stalls.keys() | _overdue_warned):
            if key[0] == slug and key not in active:
                _stalls.pop(key, None)
                _overdue_warned.discard(key)


def _warn_if_overdue(key: tuple[str, str, str], age_s: float) -> None:
    with _stall_lock:
        reason = _stalls.get(key)
        if reason is None or age_s < OVERDUE_S or key in _overdue_warned:
            return
        _overdue_warned.add(key)
    slug, task_id, chat_id = key
    logger.warning("kanban notice for %s/%s to chat %s overdue %dm: %s", slug, task_id, chat_id, age_s // 60, reason)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _board_slugs(_kb) -> list[str]:
    """One slug per distinct board DB (a pinned HERMES_KANBAN_DB aliases slugs)."""
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        try:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
        except Exception:
            return []
    unique: dict[str, str] = {}
    for meta in boards:
        slug = (meta or {}).get("slug") or _kb.DEFAULT_BOARD
        db_path = (meta or {}).get("db_path")
        try:
            ident = str(Path(db_path).expanduser().resolve() if db_path else _kb.kanban_db_path(slug).resolve())
        except Exception:
            ident = f"slug:{slug}"
        unique.setdefault(ident, slug)
    return list(unique.values())


def build_failed(session: Any) -> bool:
    """A record whose deferred agent build failed holds no agent and starts no notification poller
    (``_wire_session_agent`` never ran), so it can neither claim nor wake anything. Counting its key as loaded
    made the sweeper skip the chat for as long as the record lingered: hub chat 20261028_221818_fdf44b sat on a
    completed card's FINAL SEND after "Exact pinned route cannot fail over". The sweeper owns such a chat: its
    wake reports ``error`` and the full notice is written as a passive row, with no model call. [daveey]"""
    return (isinstance(session, dict) and session.get("agent") is None and bool(session.get("agent_error"))
            and session.get("agent_ready") is not None and session["agent_ready"].is_set())


def live_session_ids() -> set[str]:
    """Every id this process holds in memory: live sid, agent session_id, session_key (a failed build holds none)."""
    from tui_gateway import server
    ids: set[str] = set()
    with server._sessions_lock:
        for sid, session in server._sessions.items():
            if build_failed(session):
                continue
            cands = [sid]
            if isinstance(session, dict):
                cands += [getattr(session.get("agent"), "session_id", None), session.get("session_key")]
            ids.update(str(c) for c in cands if c)
    return ids


@contextlib.contextmanager
def find_session_db(chat_id: str) -> Iterator[tuple[Any, Optional[str]]]:
    """Yield ``(writable SessionDB, lineage tip id)`` for the store holding ``chat_id``, or ``(None, None)``.
    Launch store first, then each named profile that has a state.db. An unopenable store is skipped; an error
    raised by the caller's block propagates unchanged (it is never caught here: catching at the ``yield`` and
    yielding again turns it into "generator didn't stop after throw()", kanban-e5babca565)."""
    from tui_gateway import server
    with contextlib.ExitStack() as stack:
        found: tuple[Any, Optional[str]] = (None, None)
        for profile in server._readopt_wake_profiles():
            scope = contextlib.ExitStack()
            try:
                db = scope.enter_context(server._profile_db({"profile": profile} if profile else None, writer=True))
                if db is not None and db.get_session(chat_id) is not None:
                    found = (db, db.get_compression_tip(chat_id) or chat_id)
                    stack.push(scope.pop_all())  # held open until the caller's block ends
                    break
            except Exception:
                logger.debug("kanban unloaded sweep: profile %s store unavailable", profile, exc_info=True)
            with contextlib.suppress(Exception):
                scope.close()
        yield found


def wake_turn(session_id: str, text: str, metadata: dict) -> str:
    """One agent turn carrying the notice (see ``server._wake_session_turn``); its verdict string."""
    from tui_gateway import server
    return server._wake_session_turn(session_id, text, display_kind="kanban_notice", display_metadata=metadata,
                                     label="kanban-sweep-wake")


def registry_holders() -> list[dict]:
    """Live active-session leases (all backends on this host). Never raises."""
    try:
        from hermes_cli.active_sessions import active_session_registry_snapshot
        return active_session_registry_snapshot()
    except Exception:
        logger.debug("active-session registry unreadable", exc_info=True)
        return []


def _other_holder(holders: Callable[[], list[dict]], ids: set[str]) -> Optional[str]:
    """``pid N (surface@host)`` of another live backend holding one of ``ids``, else None."""
    me = os.getpid()
    for entry in holders() or ():
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if str(entry.get("session_id") or "") in ids and pid != me:
            where = "@".join(str(x) for x in (entry.get("surface"), entry.get("host")) if x)
            return f"pid {entry.get('pid')}" + (f" ({where})" if where else "")
    return None


def _has_wake_budget(chat: str, now: float) -> bool:
    limit = _env_float("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", WAKES_PER_HOUR)
    with _wake_lock:
        _wake_times[chat] = [t for t in _wake_times.get(chat, ()) if t > now - 3600]
        return len(_wake_times[chat]) < limit


def _charge_wake(chat: str, now: float) -> None:
    """Only a started turn spends budget: a refused or missing resume did not wake anything."""
    with _wake_lock:
        _wake_times.setdefault(chat, []).append(now)


def _blocked_on_human(task) -> bool:
    if task is None or getattr(task, "status", "") not in ("blocked", "triage"):
        return False
    import json

    from hermes_cli.kanban_human_request import needs_human
    request = getattr(task, "human_request", None)
    if isinstance(request, str):
        try:
            request = json.loads(request)
        except ValueError:
            return False
    return needs_human(request, kind=getattr(task, "block_kind", None))


def _wake_skip_reason(task, texts: list) -> Optional[str]:
    from gateway.warning_notifications import DiagnosticText
    if _env_float("HERMES_KANBAN_UNLOADED_WAKE", 1) <= 0:
        return "skipped:disabled"
    if _blocked_on_human(task):
        return "skipped:human"
    if all(isinstance(t, DiagnosticText) for t in texts):
        return "skipped:diagnostic"
    return None


def _spawn_thread(job: Callable[[], str]) -> str:
    from agent.local_work_agent import scope as work_scope
    from agent.memory_provider import spawn_context_thread
    # Reserve BEFORE the thread exists: spawn_context_thread then returns a TrackedThread that keeps custody.
    with work_scope("sweep", "kanban_unloaded.spawn"):
        spawn_context_thread(job, name="kanban-sweep-wake").start()  # keeps the sweeper's profile scope
    return "dispatched"


def _wake_job(wake, session_db, slug, ident, tip, text, metadata, old, new, now) -> str:
    """Run on a worker: the wake, then (unless it started) the passive row on a fresh board connection."""
    try:
        try:
            from agent.local_work_agent import scope as work_scope
            with work_scope("sweep", "kanban_unloaded.wake"):
                verdict = str(wake(tip, text, {**metadata, "woken": True}) or "error")
        except Exception:
            logger.warning("kanban sweep wake of %s failed", tip, exc_info=True)
            verdict = "error"
        if verdict == "started":
            _charge_wake(tip, now)
            from hermes_cli import kanban_db_connect as _kbc
            from hermes_cli import kanban_db_notify as _kbn
            with contextlib.closing(_kbc.connect(board=slug)) as conn:
                _kbn.ack_notify_claim(conn, upto=new, from_cursor=old, **ident)
        else:
            from hermes_cli import kanban_db_connect as _kbc
            from hermes_cli import kanban_db_notify as _kbn
            with contextlib.closing(_kbc.connect(board=slug)) as conn:
                if not _persist_notice(conn, _kbn, session_db, ident, tip, text, metadata, old, new):
                    return "deferred"
                _kbn.ack_notify_claim(conn, upto=new, from_cursor=old, **ident)
        logger.info("kanban sweep wake of %s for %s/%s: %s", tip, slug, ident["task_id"],
                    "turn started" if verdict == "started" else f"{verdict}, notice row written")
        return verdict
    except Exception:
        logger.warning("kanban sweep wake job for %s/%s failed", slug, ident["task_id"], exc_info=True)
        return "error"
    finally:
        _wake_slots.release()


def sweep_once(
    live_ids: set[str],
    *,
    grace_s: float = GRACE_S,
    now: Optional[float] = None,
    session_db: Callable[[str], Any] = find_session_db,
    wake: Callable[[str, str, dict], str] = wake_turn,
    holders: Callable[[], list[dict]] = registry_holders,
    spawn: Callable[[Callable[[], str]], str] = _spawn_thread,
) -> list[dict]:
    """One pass. Returns ``[{board, task_id, chat_id, session_id, texts, woken, wake_result, held_by}]`` for every
    notice delivered (a started turn or a persisted row)."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    from tui_gateway import server
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS
    now = time.time() if now is None else now
    delivered: list[dict] = []
    for slug in _board_slugs(_kb):
        try:
            if _kbn.count_notify_subs(board=slug, platform="tui") == 0:
                _clear_drained_stalls(slug, [])
                continue
            conn = _kbc.connect(board=slug)
        except Exception:
            continue
        with contextlib.closing(conn):
            # Claims whose holder process died before the notice reached the transcript (a serve replaced while
            # its chat was mid-turn) go back to the cursor, so this sweep or the chat's next poller delivers
            # them (t_01a1835174955959). [daveey]
            with contextlib.suppress(Exception):
                _kbn.recover_orphaned_notify_claims(conn, platform="tui")
            try:
                from tui_gateway.kanban_unloaded_candidates import subscriptions
                subs = subscriptions(conn, _KANBAN_NOTIFY_KINDS)
            except Exception:
                continue
            # Candidates omit drained rows; preserve their old stall cleanup.
            _clear_drained_stalls(slug, subs)
            for sub in subs:
                chat_id = sub.get("chat_id") or ""
                if (sub.get("platform") or "").lower() != "tui" or not chat_id or chat_id in live_ids:
                    continue
                try:
                    done = _sweep_sub(conn, slug, sub, grace_s, now, session_db, _kb, _kbn,
                                      _KANBAN_NOTIFY_KINDS, server._format_kanban_event_text, wake, holders, spawn)
                except Exception:
                    logger.warning("kanban unloaded sweep failed for %s/%s", slug, sub.get("task_id"), exc_info=True)
                    continue
                if done:
                    delivered.append(done)
    return delivered


def _sweep_sub(conn, slug, sub, grace_s, now, session_db, _kb, _kbn, kinds, fmt, wake, holders, spawn) -> Optional[dict]:
    ident = dict(task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                 thread_id=sub.get("thread_id") or "")
    key = _stall_key(slug, ident)
    _cursor, events = _kbn.unseen_events_for_sub(conn, kinds=kinds, **ident)
    if not events:
        _clear_stall(key)  # delivered (by us or a live poller): a later stall starts its own clock
        return None
    oldest = min(float(getattr(e, "created_at", 0) or 0) for e in events)
    if oldest > now - grace_s:
        return None  # a live poller still has time to take it
    done = None
    from gateway.kanban_wake_sibling import tui_defer, tui_formatter
    if tui_defer(conn, sub, now):
        return None  # the chat's waking local row still owes this event's turn; it settles this row [daveey]
    fmt = tui_formatter(conn, sub, fmt)  # a waking local row's turn carries the events it wakes on [daveey]
    try:
        done = _deliver_sub(conn, slug, sub, ident, grace_s, now, session_db, _kb, _kbn, kinds, fmt, wake, holders,
                            spawn)
        return done
    finally:
        if done is None:
            _warn_if_overdue(key, now - oldest)


def _deliver_sub(conn, slug, sub, ident, grace_s, now, session_db, _kb, _kbn, kinds, fmt, wake, holders,
                 spawn) -> Optional[dict]:
    from hermes_cli.kanban_placement import is_placeholder_session
    if is_placeholder_session(sub["chat_id"]):
        logger.warning("Dropped Kanban notice to placement placeholder %s", sub["chat_id"])
        _kbn.remove_notify_sub(conn, **ident)
        return None
    task = _kb.get_task(conn, sub["task_id"])
    with session_db(sub["chat_id"]) as (db, tip):
        if db is None or tip is None:
            # No store holds this chat (deleted): leave the cursor alone, since the chat may still appear. An
            # archived card's sub can never drain this way (its chat's poller never runs), so it kept that chat in
            # unseen counts forever (chat s1, t_01a18c03307c8661): drop it. [daveey]
            if task is not None and getattr(task, "status", "") == "archived":
                with contextlib.suppress(Exception):
                    _kbn.remove_notify_sub(conn, **ident)
                    logger.info("Dropped notify sub for archived %s/%s: no store holds chat %s",
                                slug, ident["task_id"], ident["chat_id"])
            return None
        from tui_gateway.notice_fanout import chat_skip_reason, worker_transcript_reason
        skip = chat_skip_reason(db, sub["chat_id"])  # [daveey] archived / moved copy: the notice goes nowhere
        # [daveey] A worker transcript never reads a notice and refuses the wake (4132): no row, and its sub goes.
        worker = skip is None and worker_transcript_reason(db, sub["chat_id"]) is not None
        skip = "worker_transcript" if worker else skip
        from tui_gateway.session_notifications import _KANBAN_EVENT_FORMATTERS  # the kinds that render a notice
        old, new, claimed = _kbn.claim_unseen_events_for_sub(conn, kinds=kinds, durable=skip is None,
                                                             deliverable_kinds=_KANBAN_EVENT_FORMATTERS, **ident)
        if worker:
            with contextlib.suppress(Exception):
                _kbn.remove_notify_sub(conn, **ident)
        if not claimed:
            return None  # a live poller or another sweeper got there first
        from gateway.kanban_wake_sibling import wake_owes_after
        if skip is None and wake_owes_after(conn, sub, old, now):
            # A covered event landed between tui_defer and this claim: hand it back to the wake. [daveey]
            with contextlib.suppress(Exception):
                _kbn.rewind_notify_cursor(conn, claimed_cursor=new, old_cursor=old, **ident)
            return None
        claimed = _undelivered(db, tip, slug, claimed)
    if skip is not None:
        logger.info("kanban notice for %s/%s not delivered to chat %s: %s", slug, ident["task_id"], tip, skip)
        return None
    texts = [t for t in (fmt(sub, task, ev, slug) for ev in claimed) if t]
    # Archive may carry a withdrawal receipt. Retain its subscription until
    # ack_notify_claim removes it after durable delivery; failures must retry.
    if not texts:
        # Silent kinds only (unblocked/archived): nothing to deliver, so the claim is complete. Returning without
        # this ack left the range in flight under this live process for good, and the chat's owner could never
        # claim that sub again (dashboard pid 44404, t_01a199de3003e911). [daveey]
        with contextlib.suppress(Exception):
            _kbn.ack_notify_claim(conn, upto=new, from_cursor=old, **ident)
        return None
    text = "\n".join(texts)
    metadata = {"notice_id": f"{slug}:{ident['task_id']}:{old}-{new}", "board": slug, "display_text": text,
                "task_id": ident["task_id"], "event_ids": [int(e.id) for e in claimed],
                # event-id identity shared with the live poller: a notice it already wrote is not written twice
                "kanban_event_keys": [f"{slug}:{int(e.id)}" for e in claimed if fmt(sub, task, e, slug)]}
    # [daveey] A worker's question to its orchestrator is not budgeted: a passive row nobody reads leaves the card
    # parked until its deadline (adversarial review of #3647). It cannot chain: each ask is a new park, and an
    # identical re-park escalates at kanban_awaiting.REPARK_LIMIT.
    asks = any(getattr(e, "kind", "") == "decision_requested" for e in claimed) and _ask_exempt_ok(tip, ident, now)
    held_by, verdict = _route_notice(holders, ident, tip, slug, task, texts, now, grace_s, exempt_budget=asks)
    if verdict == "skipped:slots" and _slots_may_wait(claimed, now):
        # Every wake slot is busy: hand the range back so the next pass wakes the chat, instead of a passive row
        # that no turn reads (design t_01a1d45d75851804 R4). An OVERDUE_S-old notice gets its row as before.
        with contextlib.suppress(Exception):
            _kbn.rewind_notify_cursor(conn, claimed_cursor=new, old_cursor=old, **ident)
        if _note_stall(_stall_key(slug, ident), "every wake slot is busy"):
            logger.info("kanban notice for %s/%s to chat %s deferred: every wake slot is busy; retrying next pass",
                        slug, ident["task_id"], tip)
        return None
    persisted = True
    if verdict is None:  # a worker slot is ours: the job wakes, or writes the row itself
        try:
            verdict = spawn(lambda: _wake_job(wake, session_db, slug, ident, tip, text, metadata, old, new, now))
            # The worker alone owns ack/rewind, including synchronous test dispatch.
            return {"board": slug, "task_id": ident["task_id"], "chat_id": ident["chat_id"], "session_id": tip,
                    "texts": texts, "woken": verdict == "started", "wake_result": verdict, "held_by": held_by}
        except Exception:
            _wake_slots.release()
            logger.warning("kanban sweep could not start a wake for %s", tip, exc_info=True)
            verdict = "error"
            persisted = _persist_notice(conn, _kbn, session_db, ident, tip, text, metadata, old, new)
    else:
        persisted = _persist_notice(conn, _kbn, session_db, ident, tip, text, metadata, old, new)
    if not persisted:
        return None  # chat busy: the claim is rewound and the next pass retries
    with contextlib.suppress(Exception):
        _kbn.ack_notify_claim(conn, upto=new, from_cursor=old, **ident)
    logger.info("kanban notice for unloaded session %s (board=%s task=%s events=%d wake=%s)",
                tip, slug, ident["task_id"], len(texts), verdict)
    return {"board": slug, "task_id": ident["task_id"], "chat_id": ident["chat_id"], "session_id": tip,
            "texts": texts, "woken": verdict == "started", "wake_result": verdict, "held_by": held_by}


def _slots_may_wait(events: list, now: float) -> bool:
    """A notice refused a wake slot waits for the next pass unless its oldest event is ``OVERDUE_S`` old."""
    try:
        oldest = min(float(getattr(e, "created_at", 0) or 0) for e in events)
    except ValueError:
        return False
    return now - oldest < OVERDUE_S


def _slot_wait_ok(key: tuple, now: float) -> bool:
    """A remote notice refused a wake slot may wait for the next pass until ``OVERDUE_S`` after its first refusal."""
    with _wake_lock:
        if len(_slot_waits) > 5000:
            _slot_waits.clear()
        first = _slot_waits.setdefault(key, now)
        if now - first < OVERDUE_S:
            return True
        _slot_waits.pop(key, None)
        return False


def _undelivered(db, tip, slug, claimed: list) -> list:
    """``claimed`` minus the events whose ``"<board>:<event id>"`` key a notice row in ``tip``'s lineage already
    holds (a live poller wrote it, then died before its ack). The writer refuses a range that only partly overlaps
    the lineage (KanbanNoticeOverlapError), so without this every pass rewound and retried it, and nothing was
    delivered until the chat was opened (t_01a1ce5a5b6f9bb3). A failed read keeps the whole range: the writer's
    atomic recheck still guards it, and a race after this read still raises, rewinds and retries next pass. [daveey]"""
    try:
        done = db.kanban_notice_keys_delivered(tip, [f"{slug}:{int(e.id)}" for e in claimed])
    except (sqlite3.Error, OSError):
        logger.warning("kanban unloaded sweep: delivered-key read for %s failed; offering the whole range", tip,
                       exc_info=True)
        return claimed
    return [e for e in claimed if f"{slug}:{int(e.id)}" not in done]


def _ask_exempt_ok(tip: str, ident: dict, now: float) -> bool:
    """One budget-exempt ask wake per chat and card per ``ASK_EXEMPT_S``: an orchestrator answering "not yet" and a
    re-park must not wake it without bound (the re-park also escalates at REPARK_LIMIT). Charged here."""
    key = (tip, str(ident.get("task_id") or ""))
    with _wake_lock:
        last = _ask_exempt.get(key)
        if last is not None and now - last < ASK_EXEMPT_S:
            return False
        _ask_exempt[key] = now
        return True


def _route_notice(holders, ident, tip, slug, task, texts, now, grace_s,
                  exempt_budget: bool = False) -> tuple[Optional[str], Optional[str]]:
    """``(held_by, verdict)``: verdict None means a wake slot was taken and the caller must spawn the wake; any other
    verdict says why the row is written now. Never raises: the events are already claimed."""
    held_by = None
    try:
        held_by = _other_holder(holders, {ident["chat_id"], tip})
        if held_by:
            logger.warning("kanban poller miss: chat %s is held by %s but its poller did not claim %s/%s within "
                           "%.0fs; the sweeper is delivering it", tip, held_by, slug, ident["task_id"], grace_s)
        skip = _wake_skip_reason(task, texts) or (
            None if exempt_budget or _has_wake_budget(tip, now) else "skipped:budget")
    except Exception:
        logger.warning("kanban sweep routing for %s failed; writing the notice row", tip, exc_info=True)
        return held_by, "error"
    if skip:
        return held_by, skip
    return (held_by, None) if _wake_slots.acquire(blocking=False) else (held_by, "skipped:slots")


def _persist_notice(conn, _kbn, session_db, ident, tip, text, metadata, old, new) -> bool:
    """The passive transcript row; True once written. A chat mid-turn refuses it: rewind and return False (one INFO
    line, retried next pass). Any other failure rewinds and raises, so the notice is retried, not lost."""
    from hermes_state_errors import SessionTurnLeaseLostError
    key = _stall_key(metadata["board"], ident)
    try:
        with session_db(ident["chat_id"]) as (db, _tip):
            if db is None:
                raise RuntimeError(f"chat {ident['chat_id']} disappeared before its notice was written")
            db.append_kanban_notice(tip, text, metadata)
            from hermes_state_outcome import refresh_outcomes_quietly
            refresh_outcomes_quietly([tip], "notice", db=db)  # [daveey] a passive row is the chat's last word
        return True
    except SessionTurnLeaseLostError:
        with contextlib.suppress(Exception):
            _kbn.rewind_notify_cursor(conn, claimed_cursor=new, old_cursor=old, **ident)
        first = _note_stall(key, "chat busy (a turn is running in it)")
        logger.log(logging.INFO if first else logging.DEBUG, "kanban notice for %s/%s to chat %s deferred: chat busy "
                   "(active turn); retrying next pass", key[0], key[1], tip)
        return False
    except Exception as exc:
        with contextlib.suppress(Exception):
            _kbn.rewind_notify_cursor(conn, claimed_cursor=new, old_cursor=old, **ident)
        _note_stall(key, f"{type(exc).__name__}: {exc}")
        raise


def open_subscription_sessions(session_ids: list[str]) -> dict[str, int]:
    """``{session_id: open tui subscription count}`` for the given ids, across boards. Never raises."""
    wanted = set(session_ids)
    counts: dict[str, int] = {}
    if not wanted:
        return counts
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        for slug in _board_slugs(_kb):
            with contextlib.suppress(Exception):
                if _kbn.count_notify_subs(board=slug, platform="tui") == 0:
                    continue
                with contextlib.closing(_kbc.connect(board=slug)) as conn:
                    for sub in _kbn.list_notify_subs(conn):
                        cid = sub.get("chat_id")
                        if (sub.get("platform") or "").lower() == "tui" and cid in wanted:
                            counts[cid] = counts.get(cid, 0) + 1
    except Exception:
        logger.debug("open subscription lookup failed", exc_info=True)
    return counts


class _RemoteWaker:
    """Wake policy for remote cards' notices (``kanban_remote_notices.sweep_remote_notices``), shared with local
    ones: the same skip reasons, per-chat budget, parallel-wake slots and worker thread. A notice that cannot wake
    its chat is written as the passive row. [daveey]

    Before, the remote sweep only wrote the row: on 2026-10-29 two hub cards' notices sat as rows in an idle
    David-MBP chat for 2 h and 3.5 h, until a local card's wake ran a turn that finally read them."""

    supports_defer = True  # callers pass ``defer=`` (test fakes without it keep the two-callback shape)

    def __init__(self, wake: Callable[[str, str, dict], str], spawn: Callable[[Callable[[], str]], str],
                 grace_s: float) -> None:
        self.wake, self.spawn, self.grace_s = wake, spawn, grace_s

    def deliver(self, tip: str, text: str, metadata: dict, task: dict, now: float, *,
                persist: Callable[[], bool], started: Callable[[], None],
                defer: Optional[Callable[[], Any]] = None) -> str:
        """Start one turn with ``text`` in ``tip``, else ``persist()`` the row. Returns the verdict. Never raises.
        With every wake slot busy, ``defer()`` hands the claim back for the next pass instead of the row, until the
        notice has waited ``OVERDUE_S`` for a slot (design t_01a1d45d75851804 R4)."""
        try:
            from types import SimpleNamespace
            view = SimpleNamespace(status=task.get("status"), human_request=task.get("human_request"),
                                   block_kind=task.get("block_kind"))
            skip = _wake_skip_reason(view, [text]) or (None if _has_wake_budget(tip, now) else "skipped:budget")
        except Exception:
            logger.warning("remote kanban wake routing for %s failed; writing the notice row", tip, exc_info=True)
            skip = "error"
        if skip:
            persist()
            return skip
        if not _wake_slots.acquire(blocking=False):
            if defer is not None and _slot_wait_ok((tip, metadata.get("notice_id") or text), now):
                with contextlib.suppress(Exception):
                    defer()
                return SLOTS_DEFERRED
            persist()
            return "skipped:slots"
        _slot_waits.pop((tip, metadata.get("notice_id") or text), None)

        def job() -> str:
            try:
                try:
                    verdict = str(self.wake(tip, text, {**metadata, "display_text": text, "woken": True}) or "error")
                except Exception:
                    logger.warning("remote kanban wake of %s failed", tip, exc_info=True)
                    verdict = "error"
                if verdict == "started":
                    _charge_wake(tip, now)
                    started()
                    outcome = "turn started"
                else:
                    # [daveey] Only a written row says so: persist() hands a busy chat's claim back (False).
                    outcome = f"{verdict}, notice row written" if persist() else f"{verdict}, deferred"
                logger.info("remote kanban notice for %s (task=%s): %s", tip, task.get("id"), outcome)
                return verdict
            except Exception:
                logger.warning("remote kanban wake job for %s failed", tip, exc_info=True)
                return "error"
            finally:
                _wake_slots.release()

        try:
            return self.spawn(job)
        except Exception:
            _wake_slots.release()
            logger.warning("remote kanban sweep could not start a wake for %s", tip, exc_info=True)
            persist()
            return "error"


def sweep_remote_once(live_ids: set[str], *, grace_s: float = GRACE_S, **kwargs) -> list[dict]:
    """One remote-notice pass with waking (the production entry; ``kwargs`` go to ``sweep_remote_notices``)."""
    from tui_gateway.kanban_remote_notices import sweep_remote_notices
    return sweep_remote_notices(live_ids=live_ids, waker=_RemoteWaker(wake_turn, _spawn_thread, grace_s), **kwargs)


@_la.accounted("unloaded.host_pass")
def host_pass(grace: float = GRACE_S, *, every_store: bool = True) -> None:
    """Everything this loop does for the whole install; run by the ``kanban-unloaded`` lease holder only.
    ``every_store=False`` (kill switch, lease failed open: every process runs this) retries killed-subagent
    notices in this process's own store only. Each sub-pass is an ``unloaded.<sub>`` pass (loop_accounting)."""
    from tui_gateway.kanban_unloaded_gates import profile_census
    with profile_census():
        _host_pass(grace, every_store=every_store)


def _host_pass(grace: float, *, every_store: bool) -> None:
    from tui_gateway.kanban_unloaded_gates import run
    live = live_session_ids()
    try:
        run("remote_notices", sweep_remote_once, live, grace_s=grace, live=live, every_store=every_store)
    except Exception:
        logger.warning("remote kanban unloaded sweep failed", exc_info=True)
    try:
        live = live_session_ids()  # the remote wake may have loaded a chat in this very pass
        run("notices", sweep_once, live, grace_s=grace, live=live, every_store=every_store)
    except Exception:
        logger.warning("kanban unloaded sweep failed", exc_info=True)
    # [daveey] Async-delegation results no live poller delivers (parent unloaded, loaded headless, or held by
    # another backend than the copy's): one turn in the parent, ledger-claimed (delegation_unloaded).
    try:
        from tui_gateway.delegation_unloaded import sweep_once as delegation_sweep_once
        run("delegation", delegation_sweep_once, live=live, every_store=every_store)
    except Exception:
        logger.warning("async delegation unloaded sweep failed", exc_info=True)
    # [daveey] Session outcome: refresh chats whose cards moved, then make sure no notice ends a chat unanswered.
    from tui_gateway.notice_fanout import card_change_pass
    from tui_gateway.notice_guarantee import guarantee_pass
    run("card_change_pass", card_change_pass, live=live, every_store=every_store)
    # [daveey] A parent that was mid-turn when the orphan reap killed its subagent refused that notice: re-offer
    # it before the guarantee pass, so the same tick can wake the parent on it (desktop-ac1b365935). Every
    # profile store, since one holder now retries for the whole install; without a real lease each process
    # retries its own store, as before the lease.
    if every_store:
        from tui_gateway.reap_notices import retry_pending_killed_notices_all
        run("killed_notices", retry_pending_killed_notices_all, live=live, every_store=every_store)
    else:
        from tui_gateway import server
        from tui_gateway.reap_notices import retry_pending_killed_notices
        run("killed_notices", retry_pending_killed_notices, server._get_db(), live=live, every_store=every_store)
    run("guarantee_pass", guarantee_pass, live=live, every_store=every_store)


@_la.accounted("unloaded.residual_pass")
def residual_pass() -> None:
    """A standby's pass for the chats it holds itself: the delegation and guarantee passes skip a chat another
    backend holds (``skipped:held``), so the holder never delivers those. Nothing to do without live chats."""
    live = live_session_ids()
    if not live:
        return
    try:
        from tui_gateway.delegation_unloaded import sweep_once as delegation_sweep_once
        delegation_sweep_once(only=live)
    except Exception:
        logger.warning("async delegation residual sweep failed", exc_info=True)
    from tui_gateway.notice_guarantee import guarantee_pass
    guarantee_pass(only=live)


def make_lease():
    """This process's ``kanban-unloaded`` lease (role from its process kind)."""
    from hermes_cli.host_loop_lease import HostLoopLease, process_role
    interval = max(1.0, _env_float("HERMES_KANBAN_UNLOADED_SWEEP_S", INTERVAL_S))
    return HostLoopLease(LEASE_NAME, process_role(), interval=interval)


def run_pass(lease, grace: float = GRACE_S) -> str:
    """One tick: the host pass while ``lease`` is held (then ``beat()``, also when the pass raises, so the
    watchdog keeps the lease and a ``.want`` hand-off still happens), else the residual pass. Returns which.

    Only a lease really held (``exclusive()``: not the kill switch, not failed open) gets the holder's wake
    slots and retries killed-subagent notices in every profile store; a process that runs the host pass only
    because every process does keeps its base slots and its own store, as before the lease."""
    holder = lease.held()
    exclusive = holder and lease.exclusive()
    wake_slots.set_holder(exclusive)
    if holder:
        try:
            host_pass(grace, every_store=exclusive)
        finally:
            lease.beat()
        return "host"
    from hermes_cli.host_loop_lease import host_loops_retired
    if host_loops_retired():
        # [daveey] A superseded or code-skewed backend starts no wake on its stale code, not even for the chats
        # it still holds: they move to its successor, whose passes deliver them (t_01a1d65520e78e7d).
        return "retired"
    residual_pass()
    return "residual"


def _loop(stop: threading.Event) -> None:
    interval = max(1.0, _env_float("HERMES_KANBAN_UNLOADED_SWEEP_S", INTERVAL_S))
    grace = _env_float("HERMES_KANBAN_UNLOADED_GRACE_S", GRACE_S)
    lease = make_lease()
    stop.wait(_env_float("HERMES_KANBAN_UNLOADED_FIRST_S", FIRST_RUN_DELAY_S))
    try:
        while not stop.is_set():
            try:
                run_pass(lease, grace)
            except Exception:
                logger.warning("kanban unloaded pass failed", exc_info=True)
            stop.wait(interval)
    finally:
        lease.release()


def start_kanban_unloaded_sweeper() -> Optional[threading.Event]:
    """Start the once-per-process sweeper thread. ``HERMES_KANBAN_UNLOADED_SWEEP_S=0`` disables it."""
    global _sweeper_started
    if _env_float("HERMES_KANBAN_UNLOADED_SWEEP_S", INTERVAL_S) <= 0:
        return None
    with _sweeper_lock:
        if _sweeper_started:
            return None
        _sweeper_started = True
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), daemon=True, name="kanban-unloaded-sweeper").start()
    return stop


def register(server) -> None:
    server._start_kanban_unloaded_sweeper = start_kanban_unloaded_sweeper
