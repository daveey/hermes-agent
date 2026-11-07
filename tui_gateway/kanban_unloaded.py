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
pass, the same guarantee as the live poller. Once woken, the chat is live in that process, so later notices go through its live
poller like any open chat's. Residual: if submit raises after the turn thread already started, the row is written too.
A chat mid-turn refuses the row (``SessionTurnLeaseLostError``): that is "busy, try later", so the claim is rewound
with one INFO line and the next pass retries; a notice still stalled after ``OVERDUE_S`` logs one WARNING per card
and chat with the reason (kanban-e5babca565).
[daveey]
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

GRACE_S = 30.0
INTERVAL_S = 30.0
FIRST_RUN_DELAY_S = 20.0

WAKES_PER_HOUR = 4
MAX_PARALLEL_WAKES = 2

_sweeper_started = False
_sweeper_lock = threading.Lock()
_wake_lock = threading.Lock()
_wake_times: dict[str, list[float]] = {}  # chat -> recent started wakes (this process)
_wake_slots = threading.BoundedSemaphore(MAX_PARALLEL_WAKES)

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
    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(job, name="kanban-sweep-wake").start()  # keeps the sweeper's profile scope
    return "dispatched"


def _wake_job(wake, session_db, slug, ident, tip, text, metadata, old, new, now) -> str:
    """Run on a worker: the wake, then (unless it started) the passive row on a fresh board connection."""
    try:
        try:
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
    if skip is not None:
        logger.info("kanban notice for %s/%s not delivered to chat %s: %s", slug, ident["task_id"], tip, skip)
        return None
    texts = [t for t in (fmt(sub, task, ev, slug) for ev in claimed) if t]
    if task and getattr(task, "status", "") == "archived":
        with contextlib.suppress(Exception):
            _kbn.remove_notify_sub(conn, **ident)
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
    held_by, verdict = _route_notice(holders, ident, tip, slug, task, texts, now, grace_s)
    persisted = True
    if verdict is None:  # a worker slot is ours: the job wakes, or writes the row itself
        try:
            verdict = spawn(lambda: _wake_job(wake, session_db, slug, ident, tip, text, metadata, old, new, now))
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


def _route_notice(holders, ident, tip, slug, task, texts, now, grace_s) -> tuple[Optional[str], Optional[str]]:
    """``(held_by, verdict)``: verdict None means a wake slot was taken and the caller must spawn the wake; any other
    verdict says why the row is written now. Never raises: the events are already claimed."""
    held_by = None
    try:
        held_by = _other_holder(holders, {ident["chat_id"], tip})
        if held_by:
            logger.warning("kanban poller miss: chat %s is held by %s but its poller did not claim %s/%s within "
                           "%.0fs; the sweeper is delivering it", tip, held_by, slug, ident["task_id"], grace_s)
        skip = _wake_skip_reason(task, texts) or (None if _has_wake_budget(tip, now) else "skipped:budget")
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

    def __init__(self, wake: Callable[[str, str, dict], str], spawn: Callable[[Callable[[], str]], str],
                 grace_s: float) -> None:
        self.wake, self.spawn, self.grace_s = wake, spawn, grace_s

    def deliver(self, tip: str, text: str, metadata: dict, task: dict, now: float, *,
                persist: Callable[[], bool], started: Callable[[], None]) -> str:
        """Start one turn with ``text`` in ``tip``, else ``persist()`` the row. Returns the verdict. Never raises."""
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
            persist()
            return "skipped:slots"

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
                else:
                    persist()
                logger.info("remote kanban notice for %s (task=%s): %s", tip, task.get("id"),
                            "turn started" if verdict == "started" else f"{verdict}, notice row written")
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


def _loop(stop: threading.Event) -> None:
    interval = max(1.0, _env_float("HERMES_KANBAN_UNLOADED_SWEEP_S", INTERVAL_S))
    grace = _env_float("HERMES_KANBAN_UNLOADED_GRACE_S", GRACE_S)
    stop.wait(_env_float("HERMES_KANBAN_UNLOADED_FIRST_S", FIRST_RUN_DELAY_S))
    while not stop.is_set():
        try:
            sweep_remote_once(live_session_ids(), grace_s=grace)
            sweep_once(live_session_ids(), grace_s=grace)
        except Exception:
            logger.warning("kanban unloaded sweep failed", exc_info=True)
        # [daveey] Session outcome: refresh chats whose cards moved, then make sure no notice ends a chat unanswered.
        from tui_gateway.notice_fanout import card_change_pass
        from tui_gateway.notice_guarantee import guarantee_pass
        card_change_pass()
        guarantee_pass()
        stop.wait(interval)


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
