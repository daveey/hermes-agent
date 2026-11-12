"""A transient auth/refresh bench on an OAuth account is not quota evidence for the pool."""
import time

import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from agent.model_routing_unavailable import quota_unavailable

MODEL = "claude-opus-5-5"


def _entry(i, **status):
    row = dict(id=f"acct{i}", label=f"account{i}", source="manual:device_code", auth_type="oauth",
               access_token=f"fixture-access-{i}", refresh_token=f"fixture-refresh-{i}", priority=i)
    row.update(status)
    return PooledCredential.from_dict("anthropic", row)


def _auth_benched(i, now):
    return _entry(i, last_status="exhausted", last_status_at=now, last_error_code=401,
                  last_error_reset_at=now + 300)


def _walled(i, now, days):
    return _entry(i, model_cooldowns={MODEL: now + days * 86400})


def test_auth_benched_accounts_with_quota_do_not_make_a_wall():
    """3 accounts 401-benched for 5 min + 4 with multi-day model cooldowns: the pool is not
    proven quota-exhausted (worker-3, 2026-11-12 11:32)."""
    now = time.time()
    entries = [_auth_benched(i, now) for i in range(3)]
    entries += [_walled(3 + i, now, days) for i, days in enumerate((1, 4, 5, 5.5))]
    assert quota_unavailable(CredentialPool("anthropic", entries), MODEL) is None


def test_auth_bench_on_an_account_with_its_own_quota_cooldown_still_counts_as_walled():
    """An account that is both auth-benched and model-cooled for this model is quota evidence."""
    now = time.time()
    benched = _entry(0, last_status="exhausted", last_status_at=now, last_error_code=401,
                     last_error_reset_at=now + 300, model_cooldowns={MODEL: now + 7200})
    wall = quota_unavailable(CredentialPool("anthropic", [benched, _walled(1, now, 2)]), MODEL)
    assert wall is not None
    assert wall.resets_at == pytest.approx(now + 7200, abs=1)


def test_all_429_walled_pool_still_reports_the_earliest_reset():
    now = time.time()
    entries = [_entry(0, last_status="exhausted", last_status_at=now, last_error_code=429,
                      last_error_reset_at=now + 3600), _walled(1, now, 2)]
    wall = quota_unavailable(CredentialPool("anthropic", entries), MODEL)
    assert wall is not None
    assert wall.resets_at == pytest.approx(now + 3600, abs=1)


def test_dead_and_entitlement_benched_entries_stay_excluded():
    from agent.credential_pool import STATUS_DEAD
    from agent.credential_pool_model_cooldowns import MODEL_ENTITLEMENT_BENCH_SECONDS

    now = time.time()
    entries = [_entry(0, last_status=STATUS_DEAD, last_status_at=now, last_error_code=401),
               _entry(1, model_cooldowns={MODEL: now + MODEL_ENTITLEMENT_BENCH_SECONDS}),
               _walled(2, now, 3)]
    wall = quota_unavailable(CredentialPool("anthropic", entries), MODEL)
    assert wall is not None
    assert wall.resets_at == pytest.approx(now + 3 * 86400, abs=1)
