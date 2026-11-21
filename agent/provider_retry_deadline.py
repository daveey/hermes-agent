"""[daveey] Durable provider-retry deadline: the agent side of ``turn_marker.PROVIDER_RETRY_FIELD``.

An API-error retry wait (Retry-After up to ``RETRY_AFTER_CAP_S``) used to live only in process memory, so a core-update
pause + restart could call the provider early. The gateway installs per turn (``vars(agent)``, reset in ``reset``):
``provider_retry_deadline_stamp(deadline) -> bool`` (durable, generation-fenced, read back), ``provider_retry_deadline_clear(deadline)``
and, for a logical continuation only, ``provider_retry_not_before`` restored from that turn's own marker. No callback
means no stamp: the wait stays the ordinary in-process sleep and never yields to an update pause.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from types import SimpleNamespace
import math
import time
from typing import Any

from agent import update_pause_cancel

logger = logging.getLogger(__name__)

STAMP_ATTR = "provider_retry_deadline_stamp"
CLEAR_ATTR = "provider_retry_deadline_clear"
RESTORED_ATTR = "provider_retry_not_before"
_SLICE_S = 0.2


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _attr(agent: Any, name: str) -> Any:
    try:
        return vars(agent).get(name)
    except TypeError:
        return None


def install(agent: Any, *, stamp: Any, clear: Any, restored: float | None) -> None:
    slots = vars(agent)
    slots[STAMP_ATTR], slots[CLEAR_ATTR] = stamp, clear
    if restored is not None and math.isfinite(restored):
        slots[RESTORED_ATTR] = float(restored)
    else:
        slots.pop(RESTORED_ATTR, None)


def reset(agent: Any) -> None:
    """Turn end: a cached agent reused by the next turn carries no callback and no deadline."""
    slots = vars(agent)
    for name in (STAMP_ATTR, CLEAR_ATTR, RESTORED_ATTR):
        slots.pop(name, None)


def stamp(agent: Any, deadline: float) -> bool:
    """True only when the deadline is durable (readback-verified). Any failure means: no pause for this wait."""
    fn = _attr(agent, STAMP_ATTR)
    if not callable(fn) or not math.isfinite(deadline):
        return False
    try:
        durable = fn(deadline) is True
        if durable:
            # A boundary may decline after the wait yields (Stop/job custody/pause withdrawn).
            # Keep the SAME epoch in this process until actually served, not only on disk.
            vars(agent)[RESTORED_ATTR] = float(deadline)
        return durable
    except Exception:  # health: allow BLE001 -- persistence failure keeps the ordinary wait
        logger.warning("provider retry deadline stamp failed", exc_info=True)
        return False


def _clear(agent: Any, deadline: float) -> None:
    fn = _attr(agent, CLEAR_ATTR)
    if callable(fn):
        try:
            fn(deadline)
        except Exception:  # health: allow BLE001 -- best effort; an expired deadline is inert
            logger.debug("provider retry deadline clear failed", exc_info=True)


def wait_restored(agent: Any, _retry: Any, messages: Any, api_call_count: int) -> str:
    """Before this turn's first provider/middleware call: wait out the restored epoch in this execution thread.
    Never recomputes backoff. Returns ``"done"`` (nothing left to wait / served), ``"yield"`` (the update's cancel won:
    the caller yields to the boundary pause) or ``"redirect"`` (steer restart armed). A user Stop raises
    ``InterruptedError`` (the provider path's own Stop handling)."""
    deadline = _attr(agent, RESTORED_ATTR)
    if not isinstance(deadline, float) or not math.isfinite(deadline):
        return "done"
    if _now() < deadline:
        touched = 0
        # No own-marker readback means no permission to yield, but the continuation
        # still owes its selected epoch in this process.
        can_yield = callable(_attr(agent, STAMP_ATTR))
        context = update_pause_cancel.model_request_in_flight(agent, messages, api_call_count=api_call_count) \
            if can_yield else nullcontext()
        with context:
            while _now() < deadline:
                if agent._interrupt_requested:
                    if can_yield and update_pause_cancel.model_yield_requested(agent):
                        return "yield"  # deadline stays restored + on the marker
                    if _retry is not None and agent.clear_interrupt(preserve_redirect=True):
                        _retry.restart_with_redirected_messages = True
                        return "redirect"
                    raise InterruptedError("Interrupt detected during restored provider retry wait")
                _sleep(min(_SLICE_S, max(0.0, deadline - _now())))
                touched += 1
                if touched % 150 == 0:
                    agent._touch_activity(f"provider retry wait, {int(deadline - _now())}s remaining")
    vars(agent).pop(RESTORED_ATTR, None)
    _clear(agent, deadline)
    return "done"


def before_provider_work(agent: Any, messages: Any, conversation_history: Any, api_call_count: int) -> dict | None:
    """Serve custody before hooks/compression; a declined boundary resumes the same wait.

    Redirect stays queued for the ordinary iteration boundary. Stop wins over pause.
    """
    from agent.turn_iteration_prep import _step_boundary_pause

    retry = SimpleNamespace(restart_with_redirected_messages=False)
    while True:
        try:
            # This gate runs before begin_iteration reserves the next request.
            # Tracking expects that prospective request count (and a nonempty
            # transcript even at a cold continuation's pre-prepare boundary).
            tracked_messages = messages or [{"role": "user", "content": ""}]
            verdict = wait_restored(agent, retry, tracked_messages, api_call_count + 1)
        except InterruptedError:
            from agent.interrupt_control import interrupt_issuer

            issuer = interrupt_issuer(agent)
            return {"messages": messages, "final_response": "", "interrupted": True,
                    "turn_exit_reason": f"interrupted_by_system({issuer})" if issuer else "interrupted_by_user"}
        if verdict == "done":
            return None
        if verdict == "redirect":
            continue
        update_pause_cancel.take_pending(agent)
        pause = _step_boundary_pause(agent, tracked_messages, api_call_count)
        agent.clear_interrupt(only_reason=update_pause_cancel.CANCEL_TOOL_REASON)
        if update_pause_cancel.user_stop_pending(agent):
            return {"messages": messages, "final_response": "", "interrupted": True,
                    "turn_exit_reason": "interrupted_by_user"}
        if pause:
            agent._persist_session(messages, conversation_history)
            return {"messages": messages, "final_response": "", "interrupted": True,
                    "paused_for_update": pause, "turn_exit_reason": f"paused_for_update({pause})"}
