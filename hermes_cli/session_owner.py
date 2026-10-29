"""Which gateway owns a chat session, on the Board service (session handover). [daveey]

A chat session can move to another gateway (``hermes sessions move``). The Board records the move in
``session_owners`` (hermes-hub ``POST /v2/boards/<id>/sessions``): ``{gateway, epoch, moved_at, moved_by}`` keyed by
the session's compression-lineage ROOT id. No row means no record: the session belongs to whichever gateway holds
it locally (the upstream behaviour). A move is a CAS on ``epoch`` signed by the current owner's gateway key, so two
gateways can never both believe they were handed the chat.

Fencing: every turn source on a gateway asks :func:`refusal` before it runs a turn (the TUI/Desktop admission gate
``tui_gateway/prompt_turn._admit_prompt_turn`` and the CLI resume path, ``hermes --resume`` / ``-z``). A session
whose owner record names another gateway is refused ``SESSION_MOVED``.

The Board is reached through the kanban sync client (same HTTP client and gateway key as ``/leases``); the board
is the ``default`` board's sync binding. With ``kanban.sync`` off, or that board not ``primary``, there is no
record and no fencing. Answers are cached per session for :data:`CACHE_TTL_S`. Board unreachable: the last cached
answer, stale or not; with none, the turn is allowed (fail-open) and logged.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable, Optional

_log = logging.getLogger(__name__)

CACHE_TTL_S = 30.0
MAX_IDS = 200  # the Board's per-request limit
SESSION_MOVED = "SESSION_MOVED"

_lock = threading.Lock()
_cache: dict[str, tuple[float, Optional[dict]]] = {}  # session id -> (fetched at, owner or None)
_context_cache: Optional[tuple[float, Optional[tuple[Any, str]]]] = None
_hub_factory: Optional[Callable[[], Any]] = None


class OwnerUnavailable(RuntimeError):
    """The Board cannot be asked (sync off, board not primary, or unreachable): a move cannot be recorded."""


def set_hub_factory(factory: Optional[Callable[[], Any]]) -> Optional[Callable[[], Any]]:
    """Install the Board client factory (tests: a fake Board's hub); returns the previous one. Clears the caches."""
    global _hub_factory
    previous, _hub_factory = _hub_factory, factory
    reset_cache()
    return previous


def reset_cache() -> None:
    global _context_cache
    with _lock:
        _cache.clear()
        _context_cache = None


def _board_id() -> Optional[str]:
    """The ``default`` board's service id when it syncs as ``primary``, else None (no record, no fencing)."""
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_sync as sync
    if not sync.sync_enabled():
        return None
    path = kanban_db.kanban_home() / "kanban.db"
    if not path.exists() or not sync.board_selected(str(path)):
        return None
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing(path) as conn:
        if sync.board_mode(conn) != "primary":
            return None
        board = str(sync._state(conn)["board_id"] or "")
    return board or None


def _context() -> Optional[tuple[Any, str]]:
    """``(hub, board id)`` or None when ownership is not recorded here. Cached like owner answers."""
    global _context_cache
    now = time.monotonic()
    with _lock:
        if _context_cache is not None and now - _context_cache[0] < CACHE_TTL_S:
            return _context_cache[1]
    try:
        board = _board_id()
        ctx = None
        if board:
            if _hub_factory is not None:
                hub = _hub_factory()
            else:
                from hermes_cli import kanban_sync_client as ksc
                hub = ksc.Hub(ksc.load_identity())
            ctx = (hub, board)
    except Exception as exc:  # health: allow BLE001 -- no enrollment / unreadable board: no record, no fencing
        _log.info("session owner: no Board context (%s: %s)", type(exc).__name__, exc)
        ctx = None
    with _lock:
        _context_cache = (now, ctx)
    return ctx


def _put(sid: str, owner: Optional[dict]) -> None:
    with _lock:
        _cache[sid] = (time.monotonic(), dict(owner) if owner else None)


def owners(ids: Iterable[str]) -> dict[str, Optional[dict]]:
    """``{session id: {gateway, epoch, moved_at, moved_by} | None}`` (None = no record, or unknown and fail-open)."""
    wanted = [str(i) for i in dict.fromkeys(ids) if i]
    out: dict[str, Optional[dict]] = {}
    ctx = _context()
    if ctx is None:
        return {sid: None for sid in wanted}
    hub, board = ctx
    now = time.monotonic()
    missing = []
    with _lock:
        for sid in wanted:
            hit = _cache.get(sid)
            if hit is not None and now - hit[0] < CACHE_TTL_S:
                out[sid] = dict(hit[1]) if hit[1] else None
            else:
                missing.append(sid)
    for start in range(0, len(missing), MAX_IDS):
        chunk = missing[start:start + MAX_IDS]
        try:
            res = hub.sessions(board, {"action": "get", "session_ids": chunk})
            found = res.get("owners") or {}
        except Exception as exc:  # health: allow BLE001 -- unreachable Board: last answer, else fail-open
            for sid in chunk:
                with _lock:
                    hit = _cache.get(sid)
                if hit is not None:
                    out[sid] = dict(hit[1]) if hit[1] else None
                else:
                    out[sid] = None
                    _log.warning("session owner: Board unreachable for %s (%s: %s); no cached answer, allowing the "
                                 "turn (fail-open)", sid, type(exc).__name__, exc)
            continue
        for sid in chunk:
            owner = found.get(sid) if isinstance(found, dict) else None
            owner = dict(owner) if isinstance(owner, dict) and owner.get("gateway") else None
            _put(sid, owner)
            out[sid] = owner
    return out


def owner(session_id: str) -> Optional[dict]:
    return owners([session_id]).get(str(session_id))


def move(session_id: str, to: str, expect_epoch: int, reason: str) -> dict[str, Any]:
    """Board CAS move: ``{"ok": True, "owner"}`` or ``{"ok": False, "reason", "owner"}`` (a 409 refusal).
    Raises :class:`OwnerUnavailable` when the Board is not in use here or cannot be reached."""
    ctx = _context()
    if ctx is None:
        raise OwnerUnavailable("session moves need the default board syncing to the Board service (kanban.sync, "
                               "mode primary)")
    hub, board = ctx
    try:
        res = hub.sessions(board, {"action": "move", "session_id": session_id, "to": to,
                                   "expect_epoch": int(expect_epoch), "reason": reason})
    except Exception as exc:  # health: allow BLE001 -- surfaced to the mover, nothing was recorded
        raise OwnerUnavailable(f"the Board could not record the move ({type(exc).__name__}: {exc})") from exc
    cur = res.get("owner") if isinstance(res.get("owner"), dict) else None
    _put(session_id, cur)
    if res.get("status") == "moved":
        return {"ok": True, "owner": cur}
    return {"ok": False, "reason": str(res.get("reason") or "refused"), "owner": cur}


def self_gateway() -> Optional[str]:
    from hermes_cli import kanban_capability_registry as reg
    return reg.self_gateway()


def lineage_root(session_id: str, db: Any = None) -> str:
    """The compression-lineage root of ``session_id`` (the id the Board records), or the id itself."""
    try:
        if db is None:
            from hermes_state import SessionDB
            db = SessionDB(read_only=True)
            try:
                lineage = db.get_compression_lineage(session_id)
            finally:
                db.close()
        else:
            lineage = db.get_compression_lineage(session_id)
    except Exception:  # health: allow BLE001 -- no store or no row: the id is its own root
        lineage = None
    return str(lineage[0]) if lineage else str(session_id)


def refusal(session_id: Optional[str], *, db: Any = None):
    """An ``ActiveSessionRefusal`` (reason ``SESSION_MOVED``) when the session's owner record names another
    gateway, else None. Used by every turn source (TUI admission, CLI resume)."""
    if not session_id:
        return None
    try:
        if _context() is None:
            return None
        me = self_gateway()
        if not me:
            return None
        rec = owner(lineage_root(str(session_id), db))
    except Exception as exc:  # health: allow BLE001 -- fail-open, as for an unreachable Board
        _log.warning("session owner: check failed for %s (%s: %s); allowing the turn", session_id,
                     type(exc).__name__, exc)
        return None
    if not rec or rec.get("gateway") in (None, "", me):
        return None
    from hermes_cli.active_sessions import ActiveSessionRefusal
    return ActiveSessionRefusal(f"This chat moved to {rec['gateway']}; open it there.", SESSION_MOVED)
