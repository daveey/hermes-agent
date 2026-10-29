"""Resolution failures retain route diagnostics in the gateway reply."""
from types import SimpleNamespace
from unittest.mock import Mock


def test_gateway_reply_includes_failed_fallback(monkeypatch):
    from hermes_cli import _early_recovery
    monkeypatch.setattr(_early_recovery, 'restore_interrupted_pull', lambda root: False)
    from gateway import run
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from hermes_cli.auth import AuthError
    from hermes_cli.fallback_errors import ProviderFallbackError

    chain = ProviderFallbackError([
        ('openai-codex', 'gpt-test', AuthError('primary quota exhausted', code='codex_rate_limited')),
        ('anthropic', 'claude-test', TimeoutError('fallback connection timed out')),
    ])
    wrapped = RuntimeError(str(chain))
    wrapped.__cause__ = chain
    # Upstream's run_sync resolves the source's session policy first (d8835ab3a8b7); no adapter here.
    runner = SimpleNamespace(_resolve_session_agent_runtime=Mock(side_effect=wrapped),
                             _adapter_for_source=lambda source: None)
    turn = object.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL),
                                session_key='test-session', session_id='test-session', user_config={})
    monkeypatch.setattr(turn, '_combined_ephemeral_prompt', lambda: '')
    monkeypatch.setattr(run, '_current_max_iterations', lambda: 10)
    result = turn.run_sync()
    assert 'openai-codex/gpt-test' in result['final_response']
    assert 'anthropic/claude-test' in result['final_response']
    assert run._gateway_provider_error_reply('fallback connection timed out') in result['final_response']
    assert result['api_calls'] == 0
