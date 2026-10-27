import logging
from unittest.mock import patch

import pytest
from hermes_cli.auth import AuthError
from cron.scheduler import _CronJobConfig, _resolve_job_runtime


@pytest.mark.parametrize('fallback, expected', [
    (TimeoutError('fallback connection timed out'), None),
    (AuthError('fallback quota (429); retry after 120s', code='codex_rate_limited', retry_after=120), 120),
    (AuthError('fallback quota without wait', code='codex_rate_limited'), None),
    (AuthError('bad credentials', relogin_required=True), None),
])
def test_quota_hold_requires_entire_chain(fallback, expected):
    from cron.quota_hold import hold_seconds_from_failure
    primary = AuthError('Codex quota exhausted (429); retry after 500000s',
                        provider='openai-codex', code='codex_rate_limited', retry_after=500000)
    cfg = {'fallback_providers': [{'provider': 'anthropic', 'model': 'claude-opus-5-5'}]}
    jc = _CronJobConfig(cfg, 'gpt-test', {}, '')
    with patch('hermes_cli.runtime_provider.resolve_runtime_provider',
               side_effect=[primary, fallback]), pytest.raises(RuntimeError) as caught:
        _resolve_job_runtime({}, 'test-job', jc)
    assert hold_seconds_from_failure(caught.value) == expected


def test_cron_failed_fallback_is_logged_and_surfaced(caplog):
    primary = AuthError('Codex quota exhausted (429); retry after 500000s',
                        provider='openai-codex', code='codex_rate_limited', retry_after=500000)
    cfg = {'fallback_providers': [{'provider': 'anthropic', 'model': 'claude-opus-5-5'}]}
    jc = _CronJobConfig(cfg, 'gpt-test', {'provider': 'openai-codex'}, '')
    with patch('hermes_cli.runtime_provider.resolve_runtime_provider',
               side_effect=[primary, TimeoutError('fallback connection timed out')]), \
         caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
        _resolve_job_runtime({}, 'test-job', jc)
    assert 'fallback connection timed out' in str(caught.value)
    assert str(primary) in str(caught.value)
    assert any('anthropic' in r.message and 'claude-opus-5-5' in r.message
               and 'fallback connection timed out' in r.message for r in caplog.records)
