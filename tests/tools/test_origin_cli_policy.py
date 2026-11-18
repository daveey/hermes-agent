"""Private dispatch capture cannot widen the terminal caller's grant."""
import json

import pytest


def test_no_dispatch_has_no_policy():
    from tools.origin_cli_policy import current_policy
    assert current_policy() is None


def test_real_dispatch_captures_copies_and_restores(monkeypatch):
    import model_tools
    from tools.origin_cli_policy import current_policy
    enabled = ["terminal", "kanban_create"]
    toolsets = ["terminal", "kanban"]
    disabled = ["browser"]
    seen = []
    def body(*args):
        policy = current_policy()
        seen.append(policy)
        enabled.clear()
        toolsets.clear()
        disabled.clear()
        assert policy.kwargs()["enabled_tools"] == ["terminal", "kanban_create"]
        return "{}"
    monkeypatch.setattr(model_tools, "_handle_function_call", body)
    model_tools.handle_function_call("terminal", {}, task_id="task", session_id="session",
                                    conversation_id="conversation", turn_id="turn", user_task="goal",
                                    enabled_tools=enabled, enabled_toolsets=toolsets, disabled_toolsets=disabled,
                                    skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert current_policy() is None
    assert seen[0].kwargs() == dict(task_id="task", session_id="session", conversation_id="conversation",
                                   turn_id="turn", user_task="goal", enabled_tools=["terminal", "kanban_create"],
                                   enabled_toolsets=["terminal", "kanban"], disabled_toolsets=["browser"])


def test_exception_restores_policy(monkeypatch):
    import model_tools
    from tools.origin_cli_policy import current_policy
    def body(*args):
        assert current_policy() is not None
        raise ValueError("failure")
    monkeypatch.setattr(model_tools, "_handle_function_call", body)
    with pytest.raises(ValueError):
        model_tools.handle_function_call("terminal", {})
    assert current_policy() is None


@pytest.mark.parametrize("enabled,selected", [(None, {"kanban_create"}), ([], {"kanban_create"}), (["kanban_create"], set())])
def test_policy_denies_before_dispatch(monkeypatch, enabled, selected):
    import model_tools
    from tools.origin_cli_policy import current_policy
    calls = []
    def body(*args):
        policy = current_policy()
        monkeypatch.setattr(model_tools, "handle_function_call", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr(model_tools, "_select_tool_names", lambda *a, **k: selected)
        result = policy.create({"title": "no"})
        assert not result["ok"]
        return "{}"
    monkeypatch.setattr(model_tools, "_handle_function_call", body)
    model_tools.handle_function_call("terminal", {}, enabled_tools=enabled)
    assert calls == []


def test_nested_dispatch_cannot_replace_outer_grant(monkeypatch):
    import model_tools
    from tools.origin_cli_policy import current_policy
    seen = []
    def body(name, *args):
        policy = current_policy()
        seen.append(policy)
        if name == "execute_code":
            return model_tools.handle_function_call("terminal", {}, task_id="inner")
        assert policy.kwargs()["session_id"] == "outer-session"
        assert policy.kwargs()["enabled_tools"] == ["terminal", "execute_code"]
        assert not policy.create({"title": "denied"})["ok"]
        return "{}"
    monkeypatch.setattr(model_tools, "_handle_function_call", body)
    model_tools.handle_function_call("execute_code", {}, session_id="outer-session",
                                    enabled_tools=["terminal", "execute_code"])
    assert seen[0] is seen[1]
    assert current_policy() is None


def test_nested_create_retains_identity_and_runs_all_hooks(monkeypatch):
    import model_tools
    from tools.origin_cli_policy import current_policy
    calls = []
    def body(*args):
        policy = current_policy()
        monkeypatch.setattr(model_tools, "_select_tool_names", lambda *a, **k: {"kanban_create"})
        def dispatch(name, payload, **kwargs):
            calls.append((name, payload, kwargs))
            return json.dumps({"ok": True})
        monkeypatch.setattr(model_tools, "handle_function_call", dispatch)
        assert policy.create({"title": "yes"})["ok"]
        return "{}"
    monkeypatch.setattr(model_tools, "_handle_function_call", body)
    model_tools.handle_function_call("terminal", {}, session_id="session", enabled_tools=["kanban_create"],
                                    skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
                                    skip_tool_execution_middleware=True)
    name, payload, kwargs = calls[0]
    assert name == "kanban_create" and payload == {"title": "yes"}
    assert kwargs["session_id"] == "session"
    assert not any(key.startswith("skip_") for key in kwargs)
    assert "tool_call_id" not in kwargs and "api_request_id" not in kwargs
