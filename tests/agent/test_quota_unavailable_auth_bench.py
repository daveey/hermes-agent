"""A transient auth/refresh bench on an OAuth account is not quota evidence for the pool."""
import time

import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from agent.credential_pool_disabled import set_credential_disabled
from agent.model_routing_unavailable import (
    SubscriptionQuotaUnavailable, auth_churned, quota_room, quota_unavailable,
)

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


@pytest.fixture
def disabled_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text('{"version": 1}', encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("disabled_kind", ["healthy", "earlier_wall", "auth_bench"])
def test_disabled_rows_are_neither_room_nor_wall_evidence(disabled_home, disabled_kind):
    now = time.time()
    disabled = {"healthy": _entry(0), "earlier_wall": _walled(0, now, 1),
                "auth_bench": _auth_benched(0, now)}[disabled_kind]
    pool = CredentialPool("anthropic", [disabled, _walled(1, now, 2), _walled(2, now, 3)])
    assert set_credential_disabled(pool.provider, disabled, True)
    wall = quota_unavailable(pool, MODEL)
    assert isinstance(wall, SubscriptionQuotaUnavailable)
    assert wall.resets_at == now + 2 * 86400
    assert quota_room(pool, MODEL) is False
    assert auth_churned(pool, MODEL) is False


@pytest.mark.parametrize("kind", ["healthy", "wall", "auth_bench"])
def test_all_disabled_is_not_quota_or_auth_churn(disabled_home, kind):
    now = time.time()
    entry = {"healthy": _entry(0), "wall": _walled(0, now, 1),
             "auth_bench": _auth_benched(0, now)}[kind]
    pool = CredentialPool("anthropic", [entry])
    set_credential_disabled(pool.provider, entry, True)
    assert quota_unavailable(pool, MODEL) is None
    assert quota_room(pool, MODEL) is False
    assert auth_churned(pool, MODEL) is False


def test_disabled_spare_preserves_retryable_subscription_refusal(disabled_home):
    from agent.model_routing_runtime import verify_class_runtime
    from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code

    now = time.time()
    spare, walled = _entry(0), _walled(1, now, 2)
    pool = CredentialPool("anthropic", [spare, walled])
    set_credential_disabled(pool.provider, spare, True)
    assert pool.current() is None
    runtime = {"provider": "anthropic", "api_key": walled.runtime_api_key, "credential_pool": pool}
    metadata = {"selected_provider": "anthropic", "selected_model": MODEL, "billing_group": "subscription"}
    with pytest.raises(SubscriptionQuotaUnavailable) as caught:
        verify_class_runtime(runtime, metadata)
    assert caught.value.resets_at == now + 2 * 86400
    assert turn_exit_code(None, kanban_worker=True, **credential_failure_flags(caught.value)) == 75


def test_enable_transition_rechecks_same_pool(disabled_home):
    spare = _entry(0)
    pool = CredentialPool("anthropic", [spare, _walled(1, time.time(), 2)])
    set_credential_disabled(pool.provider, spare, True)
    assert quota_room(pool, MODEL) is False
    set_credential_disabled(pool.provider, spare, False)
    assert quota_room(pool, MODEL) is True
    assert quota_unavailable(pool, MODEL) is None
