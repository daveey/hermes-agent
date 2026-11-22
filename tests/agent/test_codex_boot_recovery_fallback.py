"""A failed fast selection must retain inline, provider-scoped recovery."""
import time

import pytest

from agent import codex_boot_recovery as boot
from agent.credential_pool import load_pool
from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from hermes_cli import auth_codex
from tests.agent.test_codex_boot_recovery import construct, setup_pool as pool_fixture
from tests.hermes_cli.test_auth_codex_quota_probe import _StubClient, _StubResponse


@pytest.fixture
def setup_pool(tmp_path, monkeypatch):
    return pool_fixture.__wrapped__(tmp_path, monkeypatch)


def test_sole_exhausted_account_recovers_before_worker_exit(setup_pool, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_codex, '_codex_http_client', lambda **kw: _StubClient(
        calls, _StubResponse(200, {'rate_limit': {'primary_window': {
            'used_percent': 0, 'reset_at': time.time() + 86400 * 30}}})))
    def factory(**kw):
        selected = load_pool('openai-codex').select()
        if selected is None:
            raise SubscriptionQuotaUnavailable('openai-codex', 'fixture', time.time() + 3600)
        return selected
    selected = construct(factory)
    assert selected.last_status == 'ok'
    assert len(calls) == 1
    assert load_pool('openai-codex').select().id == selected.id


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
def test_typed_wall_retains_completion_recovery(setup_pool, monkeypatch, provider):
    monkeypatch.setattr(boot, '_start', lambda ctx: None)
    calls = []
    wall = SubscriptionQuotaUnavailable(provider, 'fixture', time.time() + 3600)
    def recovery(exc):
        calls.append(exc)
        return False
    monkeypatch.setattr('hermes_cli.kanban_quota_recovery.recover_exception', recovery)
    def factory(**kw):
        boot.defer_recovery()
        raise wall
    with pytest.raises(SubscriptionQuotaUnavailable):
        construct(factory)
    assert calls == [wall]
    assert wall._kanban_quota_probe_attempted is True


def test_recovery_survives_short_lived_process(tmp_path):
    import subprocess
    import sys
    code = '''
from pathlib import Path
import sys
import pytest
from tests.agent.test_codex_boot_recovery_fallback import (
    setup_pool, test_sole_exhausted_account_recovers_before_worker_exit,
)
with pytest.MonkeyPatch.context() as patch:
    fixture = setup_pool.__wrapped__(Path(sys.argv[1]), patch)
    test_sole_exhausted_account_recovers_before_worker_exit(fixture, patch)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    import json
    row = json.loads((tmp_path / 'home' / 'auth.json').read_text())
    assert row['credential_pool']['openai-codex'][0]['last_status'] == 'ok'


def test_nonquota_failure_is_not_marked_as_probed(setup_pool, monkeypatch):
    monkeypatch.setattr(boot, '_start', lambda ctx: None)
    error = RuntimeError('construction error')
    def factory(**kw):
        boot.defer_recovery()
        raise error
    with pytest.raises(RuntimeError):
        construct(factory)
    assert not getattr(error, '_kanban_quota_probe_attempted', False)
