"""Parent-only dispatch policy capture for native CLI launches.

This is not origin authority: strict admission independently requires live turn
custody. Neither subprocess environment nor request payload supplies this policy.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature
import json


_current = ContextVar("origin_cli_dispatch_policy", default=None)
_IDENTITY = ("task_id", "session_id", "conversation_id", "turn_id", "user_task")
_GRANTS = ("enabled_tools", "enabled_toolsets", "disabled_toolsets")


@dataclass(frozen=True)
class _Policy:
    values: tuple

    def kwargs(self):
        result = dict(self.values)
        for key in _GRANTS:
            if result[key] is not None:
                result[key] = list(result[key])
        return result

    def create(self, args):
        from model_tools import _select_tool_names, handle_function_call
        kwargs = self.kwargs()
        enabled = kwargs["enabled_tools"]
        if enabled is None or "kanban_create" not in enabled:
            return {"ok": False, "error": "kanban_create is not granted to this launch"}
        selected = _select_tool_names(kwargs["enabled_toolsets"], kwargs["disabled_toolsets"], quiet_mode=True)
        if "kanban_create" not in selected:
            return {"ok": False, "error": "kanban_create is not in this session's toolsets"}
        # New call: never inherit skip-hook flags or the terminal's call/request ID.
        return json.loads(handle_function_call("kanban_create", args, **kwargs))


def current_policy():
    return _current.get()


def capture_dispatch(function):
    """Install at the trusted dispatcher boundary, not at the terminal tool API."""
    sig = signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        # Sandbox RPC dispatch omits grants and IDs. Never replace its captured
        # outer policy with those permissive defaults.
        if _current.get() is not None:
            return function(*args, **kwargs)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        values = [(key, bound.arguments[key]) for key in _IDENTITY]
        values.extend((key, None if bound.arguments[key] is None else tuple(bound.arguments[key])) for key in _GRANTS)
        token = _current.set(_Policy(tuple(values)))
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped
