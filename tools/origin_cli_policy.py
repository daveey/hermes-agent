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

    def _release_granted(self):
        from agent.delegation_context import is_delegated_child_context
        from model_tools import _select_tool_names
        if is_delegated_child_context():
            return {"ok": False, "error": "delegated children cannot use native CLI custody"}
        kwargs = self.kwargs()
        if kwargs["enabled_tools"] is None or "terminal" not in kwargs["enabled_tools"]:
            return {"ok": False, "error": "terminal is not granted to this launch"}
        if "terminal" not in _select_tool_names(kwargs["enabled_toolsets"], kwargs["disabled_toolsets"], quiet_mode=True):
            return {"ok": False, "error": "terminal is not in this session's toolsets"}
        return None

    def release_display(self, confirmation):
        """Only an in-memory parent receipt, never child coordinates or actors."""
        refusal = self._release_granted()
        if refusal is not None:
            return refusal
        from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_sync_client as client
        from hermes_cli import kanban_release_intent as intent, kanban_release_read as read
        body = intent.validate(read.certificate.parse(confirmation.body_bytes))
        with kb.scoped_current_board(body["board"]), kbc.connect_closing() as conn:
            intent.stage(conn, body)
            from contextlib import closing
            with closing(client.Hub(client.load_identity())) as hub:
                if hub.ident.gateway != body["actor"]["gateway"]:
                    raise ValueError("native release display transport identity changed")
                return read.confirm(conn, hub, confirmation)

    def release(self, args, gate):
        """Release is captured terminal policy, not a new public tool grant."""
        refusal = self._release_granted()
        if refusal is not None:
            return refusal
        from hermes_cli.kanban_release_native import dispatch
        return dispatch(args, gate=gate)

    def create(self, args):
        from agent.delegation_context import is_delegated_child_context
        from model_tools import suppress_post_tool_call_hook
        if is_delegated_child_context():
            return {"ok": False, "error": "delegated children cannot use native CLI custody"}
        # The terminal executor owns terminal's post event, not this new call's.
        with suppress_post_tool_call_hook(False):
            return self._create(args)

    def _create(self, args):
        from model_tools import _select_tool_names, handle_function_call
        kwargs = self.kwargs()
        enabled = kwargs["enabled_tools"]
        if enabled is None or not {"kanban_create", "tool_call"}.intersection(enabled):
            return {"ok": False, "error": "kanban_create is not granted to this launch"}
        if "kanban_create" not in enabled:
            # Use the actual bridge, not a reconstructed grant. It revalidates
            # the scoped raw catalog (including drops/availability), deferral,
            # and concrete schema before redispatching through all create hooks.
            return json.loads(handle_function_call("tool_call", {
                "calls": [{"name": "kanban_create", "arguments": args}],
            }, **kwargs))
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
