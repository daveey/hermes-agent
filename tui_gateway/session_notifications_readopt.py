"""Re-adopted background jobs' exits in a Desktop backend, split from session_notifications: park the exit for its
chat's poller and resume that chat in-process when it is not open here. [daveey] Bodies are rebound onto
server.py's globals (method_ctx.bind_module), so they reference server.py globals bare."""

from __future__ import annotations

import threading

from .method_ctx import bind_module


def _readopted_exit_deliver(evt: dict, *, registry=None):
    """``ProcessRegistry.on_unknown_exit`` in a Desktop backend: park a re-adopted job's exit for its chat's
    poller, and when that chat is not open here, resume it in-process (as the startup-resume sweep does) so its
    poller takes the event and runs the completion turn. Returns the wake thread, or None. [daveey]"""
    if registry is None:
        from tools.process_registry import process_registry as registry
    registry.park_unowned_event(evt)
    key = str(evt.get("session_key") or "")
    if not key or _notif_live_session_matches({key, _notif_resolve_event_key(key)}):
        return None  # open here: its poller takes the parked event within _PARKED_POLL_SECONDS
    t = threading.Thread(target=_wake_chat_for_readopted_exit, args=(key,), daemon=True,
                         name=f"readopt-wake-{key[-6:]}")
    t.start()
    return t


def _wake_chat_for_readopted_exit(key: str):
    """Resume chat ``key`` detached in this backend; returns the profile it resumed under (None = launch), or
    None when no store has it or the resume was refused (e.g. open in another backend). Never raises."""
    import itertools as _it
    handler = _methods.get("session.resume")
    if handler is None:
        logger.warning("readopt wake: session.resume not installed; %s waits until it is opened", key)
        return None
    from tui_gateway.update_pause import readopt_wake_yields
    if readopt_wake_yields(key):  # [daveey] waited out a paused turn's hand-off and the chat is now loaded here
        return None
    for n, profile in zip(_it.count(1), _readopt_wake_profiles()):
        params = {"session_id": key, "omit_messages": True, **({"profile": profile} if profile else {})}
        try:
            env = handler(f"readopt-wake-{n}", params) or {}
        except Exception:
            logger.warning("readopt wake of %s failed", key, exc_info=True)
            return None
        err = env.get("error") if isinstance(env, dict) else None
        if not err:
            logger.info("Woke chat %s (profile=%s) for its re-adopted job's exit", key, profile or "default")
            return profile
        if not (isinstance(err, dict) and err.get("code") == 4007):
            logger.warning("readopt wake of %s refused (%s); the exit stays parked until it is opened here", key, err)
            return None
    logger.warning("readopt wake: chat %s not found in any profile store", key)
    return None


def _install_readopted_exit_waker(registry=None) -> None:
    """Desktop backend boot: route re-adopted jobs' unknown exits through ``_readopted_exit_deliver``."""
    if registry is None:
        from tools.process_registry import process_registry as registry
    registry.on_unknown_exit = lambda evt: _readopted_exit_deliver(evt, registry=registry)


def register(server) -> None:
    """Publish this module's helpers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
