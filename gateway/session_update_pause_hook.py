"""Bounded core-update pause hook for gateway-owned (TurnRunner) turns.

Installed ONLY while a scope is bound (``bind_scope``) and marked eligible, i.e. the turn is a claimed native
admission whose durable checkpoint the caller can commit. Standalone/API/cron/managed turns never bind a
scope, so they are never parked and keep counting as active work. The hook shares the TUI's request
rules (``hermes_cli.admission_fence``) and reason (``PAUSE_REASON``) and only fires at the agent's existing
completed-tool step boundary (``agent.turn_iteration_prep._step_boundary_pause``). A user Stop/new message
or a pending steer always wins.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

_HOOK_ATTR = "step_boundary_pause"
_MISSING = object()


@dataclass(frozen=True)
class UpdatePauseScope:
    """``home``: the owning profile home (a named profile resolves to its shared root for the fence);
    ``eligible``: the claimed admission may be parked (durable checkpoint is possible)."""
    home: Path
    eligible: bool = False
    resuming: bool = False
    # Omitted only by deliberate direct unit usage; production always supplies an event id.
    message_id: Any = _MISSING


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
               message_id: Any = _MISSING) -> Iterator[UpdatePauseScope]:
    scope = UpdatePauseScope(Path(home), bool(eligible), bool(resuming), message_id)
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
    prior = vars(agent).get(_HOOK_ATTR, _MISSING)
    setattr(agent, _HOOK_ATTR, boundary_hook(agent, fence_root(scope.home)))
    try:
        yield True
    finally:
        if prior is _MISSING:
            vars(agent).pop(_HOOK_ATTR, None)
        else:
            setattr(agent, _HOOK_ATTR, prior)


def is_paused(result: Any) -> bool:
    # The agent stamps the hook's reason (``PAUSE_REASON``); any other value is not our pause.
    from tui_gateway.update_pause import PAUSE_REASON
    return isinstance(result, dict) and result.get("paused_for_update") in (True, PAUSE_REASON)
