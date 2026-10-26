"""Hook workers must inherit the calling profile's ContextVars."""
from contextvars import ContextVar

from agent.pre_final_gate import _call


def test_hook_worker_inherits_context_without_leaking_changes():
    scope = ContextVar("test_profile_scope", default="wrong-profile")
    token = scope.set("target-profile")
    try:
        def callback():
            observed = scope.get()
            scope.set("worker-only")
            return observed

        assert _call(callback, {}, 5) == "target-profile"
        assert scope.get() == "target-profile"
    finally:
        scope.reset(token)
