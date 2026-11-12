"""[daveey] ``active_work`` names which gateway chat turns a core-update pause request parks.

A turn running under the gateway's update-pause hook (an eligible local/native admission: lanes and `oneshot`
reviewer chats included) parks on a durable checkpoint and auto-continues after the restart, so the updater can
pause it instead of waiting for a gap (hermes-sync ``instant_checkpoint_blockers``). Everything else stays
``update_pausable: False`` (fail closed): a pending agent, an API/automation turn, an unknown object.
"""
import types

from gateway.run import GatewayRunner
from gateway.session_update_pause_hook import bind_scope, installed, update_pausable


class _Agent:
    model = "m"

    def get_activity_summary(self):
        return {"current_tool": "terminal", "seconds_since_activity": 1.0}


def _runner(sessions):
    runner = object.__new__(GatewayRunner)
    runner._sessions_map = lambda: sessions
    runner._running_agents = {}
    runner.session_authorities = []
    runner.adapters = {}
    runner._active_api_run_count = lambda: 0
    runner._active_deferred_agent_worker_count = lambda: 0
    return runner


def _state(agent):
    return types.SimpleNamespace(turn=types.SimpleNamespace(agent=agent, started_ts=None))


def test_only_a_turn_under_the_installed_hook_is_published_pausable(tmp_path, monkeypatch):
    from gateway.run import _AGENT_PENDING_SENTINEL
    monkeypatch.setattr("gateway.session_update_pause_hook.start_mid_tool_cancel", lambda agent, root: lambda: None)
    pausable, plain = _Agent(), _Agent()
    runner = _runner({"agent:main:local:dm:a": _state(pausable), "agent:main:local:dm:b": _state(plain),
                      "agent:main:local:dm:c": _state(_AGENT_PENDING_SENTINEL)})
    with bind_scope(tmp_path, eligible=True), installed(pausable) as on:
        assert on and update_pausable(pausable) and not update_pausable(plain)
        units = {u["session"]: u for u in runner._describe_active_work() if u["kind"] == "chat"}
        assert units["agent:main:local:dm:a"]["update_pausable"] is True
        assert units["agent:main:local:dm:b"]["update_pausable"] is False
        assert units["agent:main:local:dm:c"]["update_pausable"] is False
    assert not update_pausable(pausable)  # the turn ended: no longer parkable
    with bind_scope(tmp_path, eligible=False), installed(plain) as on:
        assert not on and not update_pausable(plain)
    assert update_pausable(object()) is False and update_pausable(None) is False
