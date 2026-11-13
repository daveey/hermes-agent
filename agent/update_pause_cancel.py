"""[daveey] Mid-tool core-update cancel (t_01a1c7cf6f171e47): the agent side of ``tui_gateway.update_pause``'s
``MID_TOOL_CANCEL``.

A turn inside a long tool call never reaches a step boundary, so the core-update pause could not land. The
gateway's per-turn watcher (``update_pause.start_mid_tool_cancel``) reads which calls are running from here,
and once the grace has passed calls ``cancel``: every running call is recorded with the stamped result and
the turn is interrupted through the same path a user Stop takes (``agent.interrupt``: terminal foreground
kill, process-group interrupt, abandoned sequential worker), with a fixed ``tool_reason``. The executor
persists that stamped text as each cancelled call's result (``committed``); calls that had already finished
keep their real results. At the next step boundary ``turn_iteration_prep.begin_iteration`` takes the pending
cancel (``take_pending``) and runs the normal boundary pause hook, so the turn ends exactly as a boundary
pause does (marker kept with ``reason: core_update_pause``, reservation released, the pause announced).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

from agent.interrupt_control import _REASON_UPDATE_PAUSE, interrupt_issuer

# A fixed system category (interrupt_issuer slugs it to ``hermes_update_pause``): it never relabels a pending
# Stop, a later user Stop overrides it, and the turn then ends as that Stop instead of a pause.
CANCEL_TOOL_REASON = _REASON_UPDATE_PAUSE
_ISSUER = CANCEL_TOOL_REASON.replace(" ", "_")
_IN_FLIGHT = "_update_pause_in_flight"
_CANCEL = "_update_pause_cancel"
# The in-flight id of a context compression host wait (never a provider tool_call_id).
COMPRESSION_CALL_ID = "context-compression"


def _slots(agent: Any) -> dict:
    try:
        return vars(agent)
    except TypeError:
        return {}


def started(agent: Any, call_id: str) -> None:
    """A tool call began executing (after its pre-tool policy): it is in flight from now."""
    if call_id:
        _slots(agent).setdefault(_IN_FLIGHT, {})[call_id] = time.monotonic()


def finished(agent: Any, call_id: str) -> None:
    """The call's execution returned or raised: it keeps its real result from here on."""
    in_flight = _slots(agent).get(_IN_FLIGHT)
    if in_flight is not None:
        in_flight.pop(call_id, None)


def in_flight(agent: Any) -> dict:
    """``{tool_call_id: monotonic start}`` of the calls executing now (a copy)."""
    return dict(_slots(agent).get(_IN_FLIGHT) or {})


def cancel(agent: Any, text: str) -> list[str] | None:
    """Cancel every running call of this turn with ``text`` as its result; the sorted call ids, or None when
    nothing runs or an interrupt (a user Stop, a shutdown) is already pending, which then wins."""
    if getattr(agent, "_interrupt_requested", False):
        return None
    running = in_flight(agent)
    if not running:
        return None
    # Recorded before the interrupt: the interrupted tool commits its result only after the interrupt lands.
    _slots(agent)[_CANCEL] = {"calls": frozenset(running), "text": text}
    agent.interrupt(tool_reason=CANCEL_TOOL_REASON)
    return sorted(running)


def committed(agent: Any, call_id: str, result: Any) -> tuple[Any, bool]:
    """The call's result is being committed (it no longer runs for this turn, even when its worker was
    abandoned on a timeout). ``(stamped text, True)`` for a call this cancel interrupted while the pending
    interrupt is still the update's, else ``(result, False)``: a Stop that won keeps the call's own result."""
    finished(agent, call_id)
    record = _slots(agent).get(_CANCEL)
    if record and call_id in record["calls"] and interrupt_issuer(agent) == _ISSUER:
        # A survivable foreground command (terminal.update_survivable_processes) was not killed: it became a
        # background job, and its result names it (``process continues as <id>``). Stamping "re-run if still
        # needed" over it would hide a still-running job from the resumed turn and invite a duplicate run.
        return (result, True) if _continues_in_background(result) else (record["text"], True)
    return result, False


@contextmanager
def compression_in_flight(agent: Any) -> Iterator[None]:
    """[daveey] t_01a1d0aa52ee1404: a context compression host wait is in flight like a tool call. The watcher's
    cancel then reaches it after the same grace, and ``compression_yield_requested`` tells the host wait to stop
    the summary before its commit. Without this, a long summary (302 s on the hub) held the turn's admission
    reservation unpublished until it ended, because the pause lands only at a step boundary."""
    started(agent, COMPRESSION_CALL_ID)
    try:
        yield
    finally:
        finished(agent, COMPRESSION_CALL_ID)


# The automatic compressions that run where a yield's unchanged transcript reaches the turn loop's next step
# boundary: post-tool, and pre-API on a later iteration. Overflow recovery counts an unchanged transcript as
# exhaustion, and the turn-start / idle / engine preflights fail closed on it (review 5878929471 on #1617).
_YIELDABLE_TRIGGERS = frozenset({"post_tool", "pre_api"})


def compression_can_yield(trigger: Any, messages: Any, *, api_call_count: int) -> bool:
    """A compression may yield to the update pause only where the pause can land: a post-tool or pre-API
    compression over a transcript that ends on a tool result (``_step_boundary_pause``'s own precondition; a
    scripted prelude's tool results do not imply a model call ran). Pre-API runs after
    begin_iteration reserves the upcoming call, which is refunded before the pause boundary."""
    if api_call_count - (trigger == "pre_api") <= 0:
        return False
    if trigger not in _YIELDABLE_TRIGGERS or not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    return isinstance(last, dict) and last.get("role") == "tool"


def compression_yield_requested(agent: Any) -> bool:
    """The update's cancel covered the running compression and is still the pending interrupt (no Stop since)."""
    return _yield_requested(agent, COMPRESSION_CALL_ID)


def _yield_requested(agent: Any, call_id: str) -> bool:
    record = _slots(agent).get(_CANCEL)
    return bool(record) and call_id in record["calls"] \
        and bool(getattr(agent, "_interrupt_requested", False)) and interrupt_issuer(agent) == _ISSUER


# [daveey] t_01a1d293248f5e12: the in-flight id of a model request issued at a step boundary.
MODEL_CALL_ID = "model-request"


def model_request_can_yield(messages: Any, *, api_call_count: int) -> bool:
    """The request about to run sits at a step boundary: an earlier model call ran this turn
    (``api_call_count`` already counts this one, begin_iteration reserved it) and the transcript ends on a tool
    result, so a pause landing here loses nothing but this request (``_step_boundary_pause``'s precondition)."""
    if api_call_count - 1 <= 0 or not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    return isinstance(last, dict) and last.get("role") == "tool"


@contextmanager
def model_request_in_flight(agent: Any, messages: Any, *, api_call_count: int) -> Iterator[bool]:
    """[daveey] t_01a1d293248f5e12: a model request at a step boundary is in flight like a tool call, so the
    watcher's cancel reaches it after the same grace. Without this a long request (169 s on the hub) held the
    turn's admission reservation unpublished past the instant update's cap: a pause lands only at a step boundary.
    The turn's first request (no tool result yet) is not tracked: there is no boundary to pause at."""
    tracked = model_request_can_yield(messages, api_call_count=api_call_count)
    if tracked:
        started(agent, MODEL_CALL_ID)
    try:
        yield tracked
    finally:
        if tracked:
            finished(agent, MODEL_CALL_ID)


def model_yield_requested(agent: Any) -> bool:
    """The update's cancel covered the running model request and is still the pending interrupt (no Stop since):
    its output is discarded and the turn pauses at the boundary it was issued from."""
    return _yield_requested(agent, MODEL_CALL_ID)


def _continues_in_background(result: Any) -> bool:
    """``result`` is the terminal tool's JSON for a foreground command handed to the background."""
    if not isinstance(result, str) or '"continues_in_background"' not in result:
        return False
    try:
        parsed = json.loads(result)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("status") == "continues_in_background" \
        and bool(parsed.get("session_id"))


def take_pending(agent: Any) -> bool:
    """Consume the cancel record; True when the pending interrupt is still this cancel's (no Stop since)."""
    record = _slots(agent).pop(_CANCEL, None)
    if record is None:
        return False
    for call_id in record["calls"]:  # an abandoned worker never reports finished: it no longer runs for us
        finished(agent, call_id)
    return bool(getattr(agent, "_interrupt_requested", False)) and interrupt_issuer(agent) == _ISSUER


def user_stop_pending(agent: Any) -> bool:
    """A user's interrupt (Stop, or a new message) is pending on the agent. It beats a core-update pause of the
    turn it stops: that turn ends stopped and nothing continues it after the update (t_01a1c895eb0596b2)."""
    return bool(getattr(agent, "_interrupt_requested", False)) and interrupt_issuer(agent) is None


def reset(agent: Any) -> None:
    """Turn start: no call of an earlier turn (an abandoned worker) is in flight, and no cancel is pending."""
    slots = _slots(agent)
    slots.pop(_IN_FLIGHT, None)
    slots.pop(_CANCEL, None)
