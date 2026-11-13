"""Bounded core-update pause hook for gateway-owned (TurnRunner) turns.

Installed ONLY while a scope is bound (``bind_scope``) and marked eligible, i.e. the turn is a claimed native
admission whose durable checkpoint the caller can commit. Standalone/API/cron/managed turns never bind a
scope, so they are never parked and keep counting as active work. The hook shares the TUI's request
rules (``hermes_cli.admission_fence``) and reason (``PAUSE_REASON``) and only fires at the agent's existing
completed-tool step boundary (``agent.turn_iteration_prep._step_boundary_pause``). A user Stop/new message
or a pending steer always wins.

[daveey] A turn inside one long tool call (a reviewer chat waiting on a test run) never reaches that
boundary, so its pause could not land before the instant update's cap. As Desktop turns do
(``tui_gateway.update_pause.start_mid_tool_cancel``), an installed hook also runs a watcher that, once a
requested pause has waited ``MID_TOOL_GRACE_S`` on a running call, cancels it through the agent's
mid-tool cancel (``agent.update_pause_cancel``): the call's result becomes the stamped
``[interrupted by Hermes update ...]`` text and the turn parks at the boundary right after it.
"""
from __future__ import annotations

import _thread
import contextvars
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

_HOOK_ATTR = "step_boundary_pause"
_PAUSABLE_ATTR = "_update_pause_installed"
_MISSING = object()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdatePauseScope:
    """``home``: the owning profile home (a named profile resolves to its shared root for the fence);
    ``eligible``: the claimed admission may be parked (durable checkpoint is possible)."""
    home: Path
    eligible: bool = False
    resuming: bool = False
    # Omitted only by deliberate direct unit usage; production always supplies an event id.
    message_id: Any = _MISSING
    # [daveey] t_01a1d49d50e4c656: True while the claim would now hold this admission (``claim_hold_gate``);
    # None for a row the claim never holds (a human, a resumed checkpoint).
    claim_held: Callable[[], bool] | None = None


_scope: contextvars.ContextVar[UpdatePauseScope | None] = contextvars.ContextVar(
    "gateway_update_pause_scope", default=None)


def current_scope(message_id: Any = _MISSING) -> UpdatePauseScope | None:
    scope = _scope.get()
    if scope is not None and scope.message_id is not _MISSING:
        if message_id is _MISSING or message_id is None or message_id != scope.message_id:
            return None
    return scope


@contextmanager
def bind_scope(home: str | Path, *, eligible: bool, resuming: bool = False,
               message_id: Any = _MISSING, claim_held: Callable[[], bool] | None = None) -> Iterator[UpdatePauseScope]:
    scope = UpdatePauseScope(Path(home), bool(eligible), bool(resuming), message_id,
                             None if resuming else claim_held)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def fence_root(home: str | Path) -> Path:
    """The admission-fence root for a profile home (named profiles share the default root's fence)."""
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root(home=home)


def stop_or_steer_pending(agent: Any) -> bool:
    """A user interrupt (Stop / new message) or a buffered steer beats an update pause."""
    from agent.update_pause_cancel import user_stop_pending
    return bool(user_stop_pending(agent) or getattr(agent, "_pending_steer", None))


def boundary_hook(agent: Any, root: Path) -> Callable[[], str | None]:
    def hook() -> str | None:
        from hermes_cli import admission_fence
        from tui_gateway.update_pause import PAUSE_REASON
        if not admission_fence.pause_requested(root):  # request AND hold (is_held) both required
            return None
        if stop_or_steer_pending(agent):
            return None
        return PAUSE_REASON
    return hook


def before_turn_gate(agent: Any, claim_held: Callable[[], bool]) -> Callable[[], str | None]:
    """[daveey] t_01a1d49d50e4c656: the turn-start gate (``agent.update_pause_cancel.BEFORE_TURN_ATTR``) of a fresh
    claim the claim would now hold. A user Stop or steer pending wins, as at a boundary."""
    def gate() -> str | None:
        from tui_gateway.update_pause import PAUSE_REASON
        if not claim_held() or stop_or_steer_pending(agent):
            return None
        return PAUSE_REASON
    return gate


def mid_tool_cancel_step(agent: Any, root: Path, state: dict) -> list[str] | None:
    """One poll of ``start_mid_tool_cancel``: the cancelled call ids once a requested pause has waited
    ``MID_TOOL_GRACE_S`` on a running call (from the later of the request's first sighting and the
    call's start), else None. Never while a Stop or steer is pending: the boundary would not pause then,
    and the cut call's stamped result would only mislead the turn that goes on."""
    from agent import update_pause_cancel
    from hermes_cli import admission_fence
    from tui_gateway.update_pause import MID_TOOL_GRACE_S, interrupted_tool_result
    if not admission_fence.pause_requested(root):
        state.clear()  # a later request starts its own grace (and may cancel again)
        return None
    if state.get("cancelled") or stop_or_steer_pending(agent):
        return None  # one cancel per request: the turn pauses at the next boundary
    now = time.monotonic()
    seen = state.setdefault("seen", now)
    running = update_pause_cancel.in_flight(agent)
    if not running or now - max(seen, max(running.values())) < MID_TOOL_GRACE_S:
        return None
    cancelled = update_pause_cancel.cancel(agent, interrupted_tool_result())
    if cancelled:
        state["cancelled"] = True
        logger.info("core-update pause: gateway session %s cancelled %d running tool call(s) after %.0fs for a "
                    "Hermes update: %s", getattr(agent, "session_id", "?"), len(cancelled), MID_TOOL_GRACE_S,
                    ", ".join(cancelled))
    return cancelled


def start_mid_tool_cancel(agent: Any, root: Path) -> Callable[[], None]:
    """Per-turn daemon (one fence stat per ``MID_TOOL_POLL_S``); returns its stop."""
    from tui_gateway.update_pause import MID_TOOL_POLL_S
    stop = threading.Event()

    def run() -> None:
        state: dict = {}
        while not stop.wait(MID_TOOL_POLL_S):
            try:
                mid_tool_cancel_step(agent, root, state)
            except Exception:  # health: allow BLE001 -- a failed poll never ends the turn; the next one retries
                logger.debug("core-update mid-tool cancel poll failed", exc_info=True)

    # The low-level primitive, as the Desktop watcher: a harness that runs turn threads inline patches
    # threading.Thread, and an inline watcher would block the turn forever.
    _thread.start_new_thread(run, ())
    return stop.set


@contextmanager
def installed(agent: Any, scope: UpdatePauseScope | None = None, *,
              message_id: Any = _MISSING) -> Iterator[bool]:
    """Install the hook on ``agent`` for one run and restore its prior hook (the agent is cached).
    Yields whether it was installed; a no-op without an eligible bound scope."""
    scope = scope if scope is not None else current_scope(message_id)
    if scope is not None and scope.message_id is not _MISSING:
        if message_id is _MISSING or message_id is None or message_id != scope.message_id:
            scope = None
    if scope is None or not scope.eligible:
        yield False
        return
    from agent.update_pause_cancel import BEFORE_TURN_ATTR
    prior = vars(agent).get(_HOOK_ATTR, _MISSING)
    root = fence_root(scope.home)
    setattr(agent, _HOOK_ATTR, boundary_hook(agent, root))
    vars(agent)[_PAUSABLE_ATTR] = True
    if scope.claim_held is not None:
        vars(agent)[BEFORE_TURN_ATTR] = before_turn_gate(agent, scope.claim_held)
    stop_watcher = start_mid_tool_cancel(agent, root)
    try:
        yield True
    finally:
        stop_watcher()
        vars(agent).pop(BEFORE_TURN_ATTR, None)
        vars(agent).pop(_PAUSABLE_ATTR, None)
        if prior is _MISSING:
            vars(agent).pop(_HOOK_ATTR, None)
        else:
            setattr(agent, _HOOK_ATTR, prior)


def update_pausable(agent: Any) -> bool:
    """[daveey] Whether ``agent`` runs a turn under an installed pause hook, i.e. a core-update pause request
    parks it on a durable checkpoint (``active_work`` publishes it as ``update_pausable`` so the updater asks
    it to pause instead of waiting for a gap). Unknown objects are not pausable (fail closed)."""
    try:
        return vars(agent).get(_PAUSABLE_ATTR) is True
    except TypeError:
        return False


def is_paused(result: Any) -> bool:
    # The agent stamps the hook's reason (``PAUSE_REASON``); any other value is not our pause.
    from tui_gateway.update_pause import PAUSE_REASON
    return isinstance(result, dict) and result.get("paused_for_update") in (True, PAUSE_REASON)
