"""Resolution failures retain secondary causes across non-TUI surfaces."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import cli  # Bootstrap before the per-test home I/O guard.
import run_agent
from agent import agent_init
from hermes_cli.auth import AuthError
from hermes_cli.fallback_errors import ProviderFallbackError

CHAIN = [{"provider": "anthropic", "model": "claude-test"},
         {"provider": "deepseek", "model": "deepseek-test"}]


@pytest.mark.parametrize("surface", ["shared", "cli", "init"])
@pytest.mark.parametrize("outcome", ["exhausted", "success", "no_fallback"])
def test_resolution_chain(surface, outcome, monkeypatch, caplog):
    primary = AuthError("primary quota exhausted")
    timeout = TimeoutError("secondary connect timed out")
    final = ValueError("last route invalid")
    chain = [] if outcome == "no_fallback" else CHAIN
    caplog.set_level(logging.WARNING)
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from agent import agent_init

    agent = SimpleNamespace(provider="openai-codex", requested_provider="openai-codex",
                            model="gpt-test", _fallback_model=chain, _fallback_activated=False)
    success = {"provider": "deepseek", "api_key": "test"}
    def resolve(**kwargs):
        provider = kwargs.get("requested")
        if provider == "openai-codex":
            raise primary
        if provider == "anthropic":
            raise timeout
        if outcome == "success":
            return success
        raise final
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    monkeypatch.setattr("hermes_cli.fallback_config.resolve_entry_api_key", lambda _: None)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _: Mock(has_credentials=lambda: False))
    def resolve_client(provider, **kwargs):
        if provider == "openai-codex":
            return None, None
        result = resolve(requested=provider)
        return result, kwargs.get("model")
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", resolve_client)
    monkeypatch.setattr(agent_init, "_client_kwargs_from_routed", lambda *args: success)
    monkeypatch.setattr(agent_init, "recompute_init_fallback_api_mode", lambda *args: None)
    def run():
        if surface == "shared":
            return resolve_runtime_with_fallback({"fallback_providers": chain},
                        requested=agent.provider, target_model=agent.model)[0]
        if surface == "cli":
            return CLIAgentSetupMixin._resolve_fallback_runtime(agent, primary)
        return agent_init._routed_client_kwargs(agent, chain, 60)
    if outcome == "success":
        assert run() == success
    elif outcome == "no_fallback":
        if surface == "cli":
            assert run() is None
        else:
            with pytest.raises(Exception) as caught:
                run()
            assert not isinstance(caught.value, ProviderFallbackError)
            if surface == "shared":
                assert caught.value is primary
        return
    else:
        with pytest.raises(ProviderFallbackError) as caught:
            run()
        failures = caught.value.failures
        assert [(p, m) for p, m, _ in failures] == [
            ("openai-codex", "gpt-test"), ("anthropic", "claude-test"), ("deepseek", "deepseek-test")]
        assert failures[1][2] is timeout
        assert failures[2][2] is final
        if surface != "init":
            assert failures[0][2] is primary
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(all(s in text for s in ["anthropic", "claude-test", "TimeoutError", str(timeout)]) for text in warnings)
    if outcome == "exhausted":
        assert any(all(s in text for s in ["deepseek", "deepseek-test", "ValueError", str(final)]) for text in warnings)


def test_cli_startup_reports_secondary_failure(monkeypatch, capsys):
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    shell = CLIAgentSetupMixin()
    shell.model = "gpt-test"
    shell.requested_provider = "openai-codex"
    shell._explicit_api_key = shell._explicit_base_url = None
    shell._fallback_model = CHAIN[:1]
    shell.tool_progress_mode = "off"
    def resolve(**kwargs):
        if kwargs["requested"] == "openai-codex":
            raise AuthError("primary quota exhausted", code="rate_limited")
        raise TimeoutError("secondary connect timed out")
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    assert shell._ensure_runtime_credentials() is False
    assert "TimeoutError: secondary connect timed out" in capsys.readouterr().err
    assert shell._credentials_terminal is False


def test_gateway_retains_aggregate_cause(monkeypatch):
    from gateway import run
    cfg = {"model": {"provider": "openai-codex", "default": "gpt-test"},
           "fallback_providers": CHAIN[:1]}
    monkeypatch.setattr(run, "_load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.runtime_provider._get_model_config", lambda: cfg["model"])
    def resolve(**kwargs):
        if not kwargs.get("requested"):
            raise AuthError("primary quota exhausted")
        raise TimeoutError("secondary connect timed out")
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    with pytest.raises(RuntimeError, match="secondary connect timed out") as caught:
        run._resolve_runtime_agent_kwargs()
    assert isinstance(caught.value.__cause__, ProviderFallbackError)
    assert caught.value.__cause__.failures[0][:2] == ("openai-codex", "gpt-test")
