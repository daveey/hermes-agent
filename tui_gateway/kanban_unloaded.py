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

_sweeper_started = False
_sweeper_lock = threading.Lock()


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


def live_session_ids() -> set[str]:
    """Every id this process holds in memory: live sid, agent session_id, session_key."""
    from tui_gateway import server
    ids: set[str] = set()
    with server._sessions_lock:
        for sid, session in server._sessions.items():
            cands = [sid]
            if isinstance(session, dict):
                cands += [getattr(session.get("agent"), "session_id", None), session.get("session_key")]
            ids.update(str(c) for c in cands if c)
    return ids


@contextlib.contextmanager
def find_session_db(chat_id: str) -> Iterator[tuple[Any, Optional[str]]]:
    """Yield ``(writable SessionDB, lineage tip id)`` for the store holding ``chat_id``, or ``(None, None)``.
    Launch store first, then each named profile that has a state.db."""
    from tui_gateway import server
    from tui_gateway.session_notifications import _readopt_wake_profiles
    for profile in _readopt_wake_profiles():
        try:
            with server._profile_db({"profile": profile} if profile else None, writer=True) as db:
                if db is not None and db.get_session(chat_id) is not None:
                    yield db, (db.get_compression_tip(chat_id) or chat_id)
                    return
        except Exception:
            logger.debug("kanban unloaded sweep: profile %s store unavailable", profile, exc_info=True)
    yield None, None


def sweep_once(
    live_ids: set[str],
    *,
    grace_s: float = GRACE_S,
    now: Optional[float] = None,
    session_db: Callable[[str], Any] = find_session_db,
) -> list[dict]:
    """One pass. Returns ``[{board, task_id, chat_id, session_id, texts}]`` for every notice persisted."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS, _format_kanban_event_text
    now = time.time() if now is None else now
    delivered: list[dict] = []
    for slug in _board_slugs(_kb):
        try:
            if _kbn.count_notify_subs(board=slug, platform="tui") == 0:
                continue
            conn = _kbc.connect(board=slug)
        except Exception:
            continue
        with contextlib.closing(conn):
            try:
                subs = _kbn.list_notify_subs(conn)
            except Exception:
                continue
            for sub in subs:
                chat_id = sub.get("chat_id") or ""
                if (sub.get("platform") or "").lower() != "tui" or not chat_id or chat_id in live_ids:
                    continue
                try:
                    done = _sweep_sub(conn, slug, sub, grace_s, now, session_db, _kb, _kbn,
                                      _KANBAN_NOTIFY_KINDS, _format_kanban_event_text)
                except Exception:
                    logger.warning("kanban unloaded sweep failed for %s/%s", slug, sub.get("task_id"), exc_info=True)
                    continue
                if done:
                    delivered.append(done)
    return delivered


def _sweep_sub(conn, slug, sub, grace_s, now, session_db, _kb, _kbn, kinds, fmt) -> Optional[dict]:
    ident = dict(task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                 thread_id=sub.get("thread_id") or "")
    _cursor, events = _kbn.unseen_events_for_sub(conn, kinds=kinds, **ident)
    if not events or min(float(getattr(e, "created_at", 0) or 0) for e in events) > now - grace_s:
        return None  # nothing waiting, or a live poller still has time to take it
    task = _kb.get_task(conn, sub["task_id"])
    with session_db(sub["chat_id"]) as (db, tip):
        if db is None or tip is None:
            return None  # no store holds this chat (deleted): leave the cursor alone
        old, new, claimed = _kbn.claim_unseen_events_for_sub(conn, kinds=kinds, **ident)
        if not claimed:
            return None  # a live poller or another sweeper got there first
        texts = [t for t in (fmt(sub, task, ev, slug) for ev in claimed) if t]
        if texts:
            metadata = {"notice_id": f"{slug}:{ident['task_id']}:{old}-{new}", "board": slug,
                        "display_text": "\n".join(texts),
                        "task_id": ident["task_id"], "event_ids": [int(e.id) for e in claimed]}
            try:
                db.append_kanban_notice(tip, "\n".join(texts), metadata)
            except Exception:
                with contextlib.suppress(Exception):
                    _kbn.rewind_notify_cursor(conn, claimed_cursor=new, old_cursor=old, **ident)
                raise
    if task and getattr(task, "status", "") == "archived":
        with contextlib.suppress(Exception):
            _kbn.remove_notify_sub(conn, **ident)
    if not texts:
        return None
    logger.info("kanban notice for unloaded session %s persisted (board=%s task=%s events=%d)",
                tip, slug, ident["task_id"], len(texts))
    return {"board": slug, "task_id": ident["task_id"], "chat_id": ident["chat_id"], "session_id": tip, "texts": texts}


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


def _loop(stop: threading.Event) -> None:
    interval = max(1.0, _env_float("HERMES_KANBAN_UNLOADED_SWEEP_S", INTERVAL_S))
    grace = _env_float("HERMES_KANBAN_UNLOADED_GRACE_S", GRACE_S)
    stop.wait(_env_float("HERMES_KANBAN_UNLOADED_FIRST_S", FIRST_RUN_DELAY_S))
    while not stop.is_set():
        try:
            sweep_once(live_session_ids(), grace_s=grace)
        except Exception:
            logger.warning("kanban unloaded sweep failed", exc_info=True)
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
