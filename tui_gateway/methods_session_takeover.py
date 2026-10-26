"""Cross-process session takeover: the ``session.take_over`` RPC and the revocation watcher.

Per-session exclusivity (``hermes_cli/active_sessions.py``) refuses a chat whose lease another LIVE
process holds — e.g. a second Desktop app whose backend runs on this machine. ``session.take_over`` is
the explicit, user-initiated way past that wall: it rewrites the registry entry to this runtime
(``take_over_active_session`` records ``took_over_from``) and waits briefly for the old owner to
acknowledge. Every backend runs :func:`_takeover_watch_pass` on a daemon thread; when it finds one of
its own leases displaced it stops that runtime's turn, withdraws its open questions, tells its client,
and acknowledges. The displaced runtime's next submit re-claims and is refused ``SESSION_NOT_OWNED``.

Bodies are rebound onto server.py's globals (``method_ctx.bind_module``), so they reference server.py
globals bare (``_sessions``, ``_emit``, ``_interrupt_session_turn``, ...).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

from tui_gateway._env import env_float

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_TAKEOVER_MESSAGE = "This chat was taken over by another Hermes window."
_TAKEOVER_ERROR_SURFACE = {"layer": "gateway", "code": "SESSION_NOT_OWNED", "retryable": False}
_TAKEOVER_ACK_POLL_S = 0.25

# Registry file stamp (inode, mtime_ns, size) per path at the last watcher read: an unchanged file
# cannot hold a new takeover, so the 2 s poll costs one stat() per registry in the common case.
_takeover_watch_stamps: dict = {}
_takeover_watch_lock = threading.Lock()
_takeover_watcher_state: dict = {"started": False}


def _takeover_registry_stamp(path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _takeover_held_leases() -> list:
    """``(sid, session, lease)`` for every in-process session holding a real, unreleased lease."""
    with _sessions_lock:
        snapshot = [(sid, s, s.get("active_session_lease")) for sid, s in _sessions.items() if isinstance(s, dict)]
    return [(sid, s, lease) for sid, s, lease in snapshot
            if lease is not None and getattr(lease, "enabled", False) and not getattr(lease, "released", True)
            and getattr(lease, "state_path", None) is not None]


def _takeover_watch_pass() -> int:
    """One revocation sweep; returns how many of this process's leases were found taken over."""
    from hermes_cli.active_sessions import find_takeover_of

    with _takeover_watch_lock:
        held = _takeover_held_leases()
        by_path: dict = {}
        for item in held:
            by_path.setdefault(str(item[2].state_path), []).append(item)
        live_paths = set(by_path)
        for stale in [p for p in _takeover_watch_stamps if p not in live_paths]:
            _takeover_watch_stamps.pop(stale, None)
        revoked = 0
        for path, items in by_path.items():
            stamp = _takeover_registry_stamp(path)
            if stamp is not None and _takeover_watch_stamps.get(path) == stamp:
                continue
            _takeover_watch_stamps[path] = stamp
            for sid, session, lease in items:
                try:
                    taker = find_takeover_of(lease.lease_id, state_path=path)
                except Exception:
                    logger.debug("takeover probe failed for %s", path, exc_info=True)
                    continue
                if taker is not None and _revoke_taken_over_lease(sid, session, lease, taker):
                    revoked += 1
        return revoked


def _revoke_taken_over_lease(sid: str, session: dict, lease, taker: dict) -> bool:
    """Stand this runtime down: its registry entry is already gone (never touch the new owner's)."""
    from hermes_cli.active_sessions import acknowledge_takeover

    with _sessions_lock:
        if lease.released or session.get("active_session_lease") is not lease:
            return False
        lease.released = True
        session.pop("active_session_lease", None)
        session["_lease_taken_over"] = True  # finalize must not end the row the new owner is writing
    logger.warning("Session %s (%s) was taken over by pid=%s surface=%s; stopping this runtime",
                   sid, lease.session_id, taker.get("pid"), taker.get("surface"))
    try:
        _interrupt_session_turn(sid, session, request_id="lease-taken-over")
    except Exception:
        logger.exception("takeover interrupt failed sid=%s", sid)
    with contextlib.suppress(Exception):
        from tui_gateway import server_requests
        server_requests.cancel(sid, reason="taken_over")  # unblock an agent parked on clarify/approval/sudo
    with contextlib.suppress(Exception):
        from hermes_cli.active_sessions import session_owner_details
        message = f"{_TAKEOVER_MESSAGE}\n{session_owner_details(lease.session_id, taker)}"
        _emit("error", sid, {"message": message, "error_surface": dict(_TAKEOVER_ERROR_SURFACE)})
    try:
        acknowledge_takeover(lease.lease_id, state_path=lease.state_path)
    except Exception:
        logger.exception("takeover acknowledgement failed sid=%s", sid)
    return True


def _start_takeover_watcher() -> bool:
    """Start the daemon revocation watcher once per process (``HERMES_SESSION_TAKEOVER_POLL_S``, default
    2 s; <= 0 disables). Off under the test harness (``HERMES_TEST_ISOLATION``): tests drive
    :func:`_takeover_watch_pass` directly and a background pass would race their assertions."""
    from agent.memory_provider import spawn_context_thread

    poll = env_float("HERMES_SESSION_TAKEOVER_POLL_S", 2.0)
    if _takeover_watcher_state["started"] or poll <= 0 or os.environ.get("HERMES_TEST_ISOLATION"):
        return False
    _takeover_watcher_state["started"] = True

    def _loop():
        while True:
            time.sleep(poll)
            try:
                _takeover_watch_pass()
            except Exception:
                logger.debug("session takeover watcher pass failed", exc_info=True)

    spawn_context_thread(_loop, name="session-takeover-watch").start()
    return True


def _await_takeover_ack(lease, removed: list) -> bool:
    """Wait (``HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S``, default 8 s) for every displaced owner to ack."""
    from hermes_cli.active_sessions import takeover_acknowledged

    if not removed:
        return True
    deadline = time.monotonic() + max(0.0, env_float("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", 8.0))
    while True:
        with contextlib.suppress(Exception):
            if takeover_acknowledged(lease, removed):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_TAKEOVER_ACK_POLL_S)


def _reload_history_after_takeover(session: dict) -> bool:
    """Re-read the transcript from state.db so the next turn sees the rows the old owner wrote."""
    if session.get("running"):
        return False
    rows = _load_durable_truncation_history(session, repair_alternation=True)
    if rows is None:
        return False
    history = canonicalize_replay_history(rows)
    with session["history_lock"]:
        if session.get("running"):
            return False
        session["history"] = history
        session["history_version"] = int(session.get("history_version", 0)) + 1
    return True


def _takeover_previous_owner(entry: dict | None) -> dict | None:
    if not entry:
        return None
    pid = entry.get("pid")
    started = entry.get("started_at")
    return {"surface": str(entry.get("surface") or "unknown"),
            "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "started_at": float(started) if isinstance(started, (int, float)) else None}


def _take_over_durable_writer(rid, sid: str, session: dict, owner: dict):
    from pathlib import Path
    from hermes_cli.active_sessions import (
        request_turn_handoff, take_over_active_session, turn_handoff_is_current, _state_path)
    # Bind to the store we inspected, not the RPC thread's ambient profile.
    with _session_db(session) as db:
        registry_home = Path(db.db_path).parent
    lease = session.get("active_session_lease")
    if lease is not None and Path(lease.state_path).resolve() != _state_path(registry_home).resolve():
        return _err(rid, 4090, _SESSION_OWNERSHIP_UNAVAILABLE,
                    {"reason": "SESSION_COORDINATION_UNAVAILABLE"})
    if lease is None:
        lease, _removed, refusal = take_over_active_session(
            session_id=str(session.get("session_key") or ""), surface=_session_source(session),
            metadata=_lease_metadata(sid), registry_home=registry_home,
            config=_load_cfg(), track_liveness=True)
        if refusal is not None:
            return _err(rid, 4090, str(refusal), {"reason": getattr(refusal, "reason", None)})
        _attach_lease(session, lease)
    if not request_turn_handoff(lease, owner["holder"]):
        return _err(rid, 4090, _SESSION_OWNERSHIP_UNAVAILABLE,
                    {"reason": "SESSION_COORDINATION_UNAVAILABLE"})
    deadline = time.monotonic() + max(0.0, env_float("HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S", 8.0))
    while True:
        current = _foreign_turn_owner(session)
        if (session.get("active_session_lease") is not lease
                or not turn_handoff_is_current(lease, owner["holder"])
                or (current is not None and current["holder"] != owner["holder"])):
            return _err(rid, 4090, "Ownership changed during handoff. Retry Take over here.",
                        {"reason": "SESSION_NOT_OWNED"})
        if current is None:
            break
        if time.monotonic() >= deadline:
            return _err(rid, 4090,
                        "The other writer has not released this chat. It may be an older Hermes CLI. "
                        "Stop its turn in that terminal, then retry Take over here. No writer was forced out.",
                        {"reason": "SESSION_NOT_OWNED"})
        time.sleep(_TAKEOVER_ACK_POLL_S)
    _reload_history_after_takeover(session)
    holder = owner["holder"]
    surface = holder.rsplit(":platform=", 1)[-1] if ":platform=" in holder else "unknown"
    return _ok(rid, {"status": "taken_over", "acknowledged": True,
                     "previous_owner": {"surface": surface,
                                        "pid": int(holder.split(":", 1)[0][4:])
                                        if holder.startswith("pid=") and holder.split(":", 1)[0][4:].isdigit() else None,
                                        "started_at": owner["acquired_at"]}})


@method("session.take_over")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    sid = str(params.get("session_id") or "")
    try:
        owner = _foreign_turn_owner(session)
        if owner is not None:
            return _take_over_durable_writer(rid, sid, session, owner)
    except Exception:
        logger.warning("Durable writer handoff failed", exc_info=True)
        return _err(rid, 4090, _SESSION_OWNERSHIP_UNAVAILABLE,
                    {"reason": "SESSION_COORDINATION_UNAVAILABLE"})
    if session.get("active_session_lease") is not None:
        return _ok(rid, {"status": "already_owner", "previous_owner": None, "acknowledged": True})
    key = str(session.get("session_key") or "")
    surface = _session_source(session)
    if key and _take_over_detached_runtime_lease(sid, session, key):
        _reload_history_after_takeover(session)
        return _ok(rid, {"status": "taken_over", "acknowledged": True,
                         "previous_owner": {"surface": surface, "pid": os.getpid(), "started_at": None}})
    from hermes_cli.active_sessions import take_over_active_session
    try:
        lease, removed, refusal = take_over_active_session(
            session_id=key, surface=surface, metadata=_lease_metadata(sid),
            registry_home=session.get("profile_home"), config=_load_cfg(),
            track_liveness=str(surface or "").strip().lower() == "desktop")
    except Exception:  # health: allow BLE001 -- any registry/lock failure must fail CLOSED as a refusal, logged with traceback
        logger.warning("session.take_over failed for %s", key, exc_info=True)
        return _err(rid, 4090, _SESSION_OWNERSHIP_UNAVAILABLE, {"reason": "SESSION_COORDINATION_UNAVAILABLE"})
    if refusal is not None:
        reason = getattr(refusal, "reason", None)
        return _err(rid, 4090, str(refusal), {"reason": reason} if reason else None)
    _attach_lease(session, lease)
    with contextlib.suppress(Exception):
        _takeover_watch_pass()  # a displaced same-process sibling stands down now, not on the next tick
    acknowledged = _await_takeover_ack(lease, removed)
    try:
        _reload_history_after_takeover(session)
    except Exception:
        logger.warning("session.take_over: history reload failed for %s", key, exc_info=True)
    return _ok(rid, {"status": "taken_over", "acknowledged": acknowledged,
                     "previous_owner": _takeover_previous_owner(removed[0] if removed else None)})


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals), install the handler, and
    start the revocation watcher."""
    bind_module(globals(), server, skip=("_",))
    server._start_takeover_watcher()
