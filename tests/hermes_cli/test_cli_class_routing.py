"""CLI lazy credentials must preserve class policy through agent construction."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cli  # load bootstrap before the real-home guard
from run_agent import AIAgent
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from tests.agent import test_model_routing_agent

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


def shell():
    return SimpleNamespace(
        model="class:deep", requested_provider="auto", session_id="cli-session",
        _explicit_api_key=None, _explicit_base_url=None, agent=None,
        api_key=None, base_url=None, provider="auto", api_mode="chat_completions",
        acp_command=None, acp_args=[], tool_progress_mode="off",
        _resolve_fallback_runtime=MagicMock(), _session_db=MagicMock())


def test_cli_class_credentials_and_agent_resume(routing_home, streaming_route):
    from hermes_cli.cli_model_routing import agent_route
    s = shell()
    assert CLIAgentSetupMixin._ensure_runtime_credentials(s)
    assert s.model == "class:deep"
    assert s.provider == "anthropic"
    assert s._credential_pool.current().auth_type == "oauth"
    model, runtime = agent_route(s.model, {"provider": s.provider, "api_key": s.api_key,
                                           "base_url": s.base_url})
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        first = AIAgent(model=model, session_id=s.session_id, quiet_mode=True,
                        skip_context_files=True, skip_memory=True, **runtime)
        s.agent = first
        assert CLIAgentSetupMixin._ensure_runtime_credentials(s)
        assert s.agent is first
        resumed = AIAgent(model=model, session_id=s.session_id, quiet_mode=True,
                          skip_context_files=True, skip_memory=True, **runtime)
    assert first.model == resumed.model == "anthropic-deep"
    assert first.model_routing == resumed.model_routing
    s._resolve_fallback_runtime.assert_not_called()


def test_cli_empty_class_never_uses_legacy_fallback(routing_home, streaming_route, monkeypatch):
    monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **k: False)
    s = shell()
    assert CLIAgentSetupMixin._probe_runtime_credentials(s)[0] is False
    assert CLIAgentSetupMixin._ensure_runtime_credentials(s) is False
    s._resolve_fallback_runtime.assert_not_called()


def test_cli_real_construction_request_and_database_resume(routing_home, streaming_route, tmp_path):
    import json
    import anthropic
    import httpx
    from hermes_state import SessionDB

    sent = []

    def respond(request):
        sent.append((str(request.url), dict(request.headers), json.loads(request.content)))
        message = {
            "id": "test-message", "type": "message", "role": "assistant",
            "model": "anthropic-deep", "content": [],
            "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"}]
        body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    sdk_class = anthropic.Anthropic

    def sdk(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
        return sdk_class(**kwargs)

    db = SessionDB(tmp_path / "cli-test.db")
    with patch("cli._prepare_deferred_agent_startup"), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build"), \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("anthropic.Anthropic", side_effect=sdk):
        s = cli.HermesCLI(model="class:deep", provider="auto", compact=True)
        s.session_id = "cli-real"
        s._session_db = db
        s._install_tool_callbacks = lambda: None
        s.streaming_enabled = False
        s.ignore_rules = True
        assert s._init_agent()
        assert s.agent.model_routing["requested_class"] == "deep"
        signature = s._resolve_turn_agent_config("next")["signature"]
        assert signature == s._active_agent_route_signature
        result = s.agent.run_conversation("Say ok")
        assert result["final_response"] == "ok"
        stored = db.get_session(s.session_id)
        restored = shell()
        restored._explicit_model_override = False
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        CLIModelSwitchMixin._restore_session_model(restored, stored, quiet=True)
        assert restored.model == "class:deep"
        assert restored.requested_provider == "auto"
        s.agent.switch_model(s.agent.model, s.agent.provider, api_key=s.agent.api_key,
                             base_url=s.agent.base_url, api_mode=s.agent.api_mode)
        config = db.get_session(s.session_id)["model_config"]
        config = json.loads(config) if isinstance(config, str) else config
        assert not config.get("model_routing")
        assert not s.agent._session_init_model_config.get("model_routing")
        s.agent.release_clients()
    db.close()
    assert sent
    assert all(body["model"] == "anthropic-deep" for _, _, body in sent)
    assert all(url.startswith("https://api.anthropic.com/") for url, _, _ in sent)
    assert all(headers.get("authorization", "").startswith("Bearer sk-ant-oat") for _, headers, _ in sent)
    assert all("x-api-key" not in headers for _, headers, _ in sent)


def test_cli_pin_is_not_rewritten():
    from hermes_cli.cli_model_routing import agent_route
    runtime = {"provider": "custom", "api_key": "test", "base_url": "http://localhost:8000"}
    assert agent_route("unknown-exact-pin", runtime) == ("unknown-exact-pin", runtime)


def test_cli_restore_class_metadata_and_explicit_override():
    from hermes_cli.cli_model_routing import restore_class_route
    s = shell()
    s.model = "ambient-pin"
    metadata = {"model": "anthropic-deep", "model_config": {
        "model_routing": {"requested_class": "deep", "selected_model": "anthropic-deep"}}}
    assert restore_class_route(s, metadata)
    assert s.model == "class:deep"
    assert s.requested_provider == "auto"
    s._explicit_model_override = True
    s.model = "explicit-pin"
    assert not restore_class_route(s, metadata)
    assert s.model == "explicit-pin"
