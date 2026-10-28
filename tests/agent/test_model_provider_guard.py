"""[daveey] A bare vendor model id (``claude-opus-5-5``) on a vendor/model-namespaced aggregator
(OpenRouter, Nous Portal) can only 400 ("is not a valid model ID"). The agent must refuse it at
init, before any request, with an error that names the provider, base URL, model and how the
route was chosen, instead of sending the request (self-heal agent-1572c64e24)."""

import logging

import pytest


def _agent(**kw):
    from run_agent import AIAgent

    params = dict(
        model="claude-opus-5-5",
        provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    params.update(kw)
    return AIAgent(**params)


def test_bare_claude_on_openrouter_is_refused_before_any_request(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.model_provider_guard"):
        with pytest.raises(ValueError) as exc:  # today: no error, the 400-bound agent is built
            _agent()
    assert type(exc.value).__name__ == "ModelProviderMismatchError"
    msg = str(exc.value)
    assert "openrouter" in msg
    assert "https://openrouter.ai/api/v1" in msg
    assert "claude-opus-5-5" in msg
    assert "anthropic/claude-opus-5-5" in msg  # the fix it suggests
    assert "route: pinned" in msg
    from hermes_cli.cli_chat_error_copy import agent_init_failure_message
    shown = agent_init_failure_message(exc.value)  # what `hermes chat` prints: first line only
    assert "anthropic/claude-opus-5-5" in shown and "openrouter" in shown and "…" not in shown
    assert any("claude-opus-5-5" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_auto_route_that_lands_on_openrouter_is_named_auto():
    """The incident route: provider ``auto`` resolved to OpenRouter while the model stayed bare."""
    from agent.model_provider_guard import ModelProviderMismatchError

    with pytest.raises(ModelProviderMismatchError) as exc:
        _agent(requested_provider="auto")
    assert "route: auto" in str(exc.value)


def test_bare_claude_on_nous_is_refused():
    from agent.model_provider_guard import ModelProviderMismatchError

    with pytest.raises(ModelProviderMismatchError):
        _agent(provider="nous", base_url="https://inference-api.nousresearch.com/v1")


@pytest.mark.parametrize("model", ["anthropic/claude-opus-5-5", "openrouter/auto", "some-house-model"])
def test_namespaced_or_unknown_ids_pass(model):
    agent = _agent(model=model)
    assert agent.model == model


def test_direct_anthropic_keeps_bare_claude():
    agent = _agent(provider="anthropic", base_url="https://api.anthropic.com", api_key="sk-ant-dummy")
    assert agent.provider == "anthropic"


def test_config_switch_turns_the_guard_off():
    """``agent.model_provider_guard: false`` in the profile's config.yaml, read by the real loader."""
    import os
    from pathlib import Path

    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("agent:\n  model_provider_guard: false\n", encoding="utf-8")
    agent = _agent()
    assert agent.model == "claude-opus-5-5"
