import logging
from unittest.mock import patch

import pytest

from hermes_cli.auth import AuthError
from tui_gateway import server
from tui_gateway.user_messages import agent_init_failed_message


def test_failed_fallback_is_logged_and_surfaced(caplog):
    primary = AuthError('Codex quota exhausted (429); retry after 500000s',
                        provider='openai-codex', code='codex_rate_limited', retry_after=500000)
    chain = [{'provider': 'anthropic', 'model': 'claude-opus-5-5'}]
    with patch.object(server, '_load_fallback_model', return_value=chain), patch(
        'hermes_cli.runtime_provider.resolve_runtime_provider',
        side_effect=[primary, TimeoutError('fallback connection timed out')],
    ), caplog.at_level(logging.WARNING), pytest.raises(AuthError) as caught:
        server._resolve_runtime_with_fallback()
    assert 'fallback connection timed out' in str(caught.value)
    assert str(primary) in agent_init_failed_message(caught.value)
    assert 'fallback connection timed out' in agent_init_failed_message(caught.value)
    assert any('anthropic' in r.message and 'claude-opus-5-5' in r.message
               and 'fallback connection timed out' in r.message for r in caplog.records)
