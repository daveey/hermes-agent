"""Direct/oneshot chat must restore intent before snapshotting the turn route."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin


@pytest.mark.parametrize("explicit_pin", [False, True])
def test_chat_restores_before_route_and_credentials(explicit_pin):
    # Stop at construction: this exercises the real chat entry ordering without
    # a provider call. The restore adapter itself owns explicit-override handling.
    shell = SimpleNamespace(
        _resumed=True, _session_db=object(), conversation_history=[],
        _secret_capture_callback=lambda *a: None, _active_agent_route_signature="restored",
        agent=None, model="launch-default", _explicit_model_override=explicit_pin)
    calls = []

    def restore():
        calls.append("restore")
        shell.conversation_history = [{"role": "user", "content": "history"}]
        if not explicit_pin:
            shell.model = "stored-model"
        return True

    def credentials():
        calls.append("credentials")
        assert shell.conversation_history
        return True

    def route(message):
        calls.append("route")
        assert shell.model == ("launch-default" if explicit_pin else "stored-model")
        return {"signature": "restored", "model": shell.model, "runtime": {}}

    shell._load_resumed_history_late = restore
    shell._ensure_runtime_credentials = credentials
    shell._resolve_turn_agent_config = route
    shell._init_agent = Mock(return_value=False)
    assert CLIChatTurnMixin.chat(shell, "hello") is None
    assert calls == ["restore", "credentials", "route"]
    assert shell._init_agent.call_args.kwargs["model_override"] == shell.model


def test_chat_does_not_resolve_or_send_when_restore_refuses():
    shell = SimpleNamespace(
        _resumed=True, _session_db=object(), conversation_history=[],
        _secret_capture_callback=lambda *a: None,
        _load_resumed_history_late=Mock(return_value=False),
        _ensure_runtime_credentials=Mock(), _resolve_turn_agent_config=Mock())
    assert CLIChatTurnMixin.chat(shell, "hello") is None
    shell._ensure_runtime_credentials.assert_not_called()
    shell._resolve_turn_agent_config.assert_not_called()
