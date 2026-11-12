"""Verified recoveries are scoped, generation-checked store transactions."""
import time

import pytest

from tests.agent import test_anthropic_model_cooldowns as helpers

pool = helpers.pool
A, B, KEY = helpers.MODEL_A, helpers.MODEL_B, helpers.KEY


def bench(pool, model=A):
    pool.mark_exhausted_and_rotate(status_code=429, api_key_hint=KEY,
        model=model, failure_reason="rate_limit", error_context={"reset_at": time.time() + 3600})
    return pool.entries()[0]


def test_verified_recovery_clears_only_observed_model(pool):
    from agent.credential_pool import load_pool
    from hermes_cli.auth import read_credential_pool

    bench(pool, B)
    observed = bench(pool)
    stale = load_pool("anthropic")
    assert pool.clear_verified_model_cooldown(observed, A)
    row = read_credential_pool("anthropic")[0]
    assert A not in row["model_cooldowns"]
    assert B in row["model_cooldowns"]
    stale._persist()
    assert A not in read_credential_pool("anthropic")[0]["model_cooldowns"]
    assert A not in stale.entries()[0].model_cooldowns
    assert pool.select(model=A) is not None
    assert pool.select(model=B) is None


def test_new_failure_after_clear_still_benches(pool):
    from agent.credential_pool import load_pool
    from hermes_cli.auth import read_credential_pool

    observed = bench(pool)
    stale = load_pool("anthropic")
    assert pool.clear_verified_model_cooldown(observed, A)
    bench(stale)
    pool._persist()
    assert A in read_credential_pool("anthropic")[0]["model_cooldowns"]
    assert pool.select(model=A) is None


@pytest.mark.parametrize("change", ["token", "refresh", "until", "stamp", "cooldown_until",
                                     "dead", "billing", "disabled"])
def test_verified_recovery_refuses_changed_or_unsafe_row(pool, change):
    from hermes_cli import auth
    from agent.credential_pool_disabled import set_credential_disabled

    observed = bench(pool)
    if change == "disabled":
        set_credential_disabled("anthropic", observed, True)
    else:
        with auth._auth_store_lock():
            store = auth._load_auth_store()
            row = store["credential_pool"]["anthropic"][0]
            updates = {
                "token": {"access_token": "peer-token"},
                "refresh": {"refresh_token": "peer-refresh"},
                "until": {"model_cooldowns": {A: row["model_cooldowns"][A] + 1}},
                "stamp": {"model_cooldown_recorded_at": {A: row["model_cooldown_recorded_at"][A] + 1}},
                "cooldown_until": {"cooldown_until": time.time() + 7200},
                "dead": {"last_status": "dead"},
                "billing": {"last_status": "exhausted", "last_error_code": 402, "last_error_reason": "billing"},
            }
            row.update(updates[change])
            auth._save_auth_store(store)
    before = auth.read_credential_pool("anthropic")
    assert not pool.clear_verified_model_cooldown(observed, A)
    assert auth.read_credential_pool("anthropic") == before


def test_codex_recovery_preserves_other_model_benches(pool):
    from agent.credential_pool import CredentialPool, PooledCredential
    from hermes_cli import auth

    row = pool.entries()[0].to_dict()
    row.update(last_status="exhausted", last_status_at=time.time() - 60,
               last_error_code=429, last_error_reset_at=time.time() + 3600,
               model_cooldowns={B: time.time() + 7200})
    auth.write_credential_pool("openai-codex", [row])
    codex = CredentialPool("openai-codex", [PooledCredential.from_dict("openai-codex", row)])
    observed = codex.entries()[0]
    assert codex.clear_verified_model_cooldown(observed, A)
    recovered = auth.read_credential_pool("openai-codex")[0]
    assert recovered["last_status"] == "ok"
    assert B in recovered["model_cooldowns"]
    # A stale row cannot resurrect the account-wide 429 either.
    auth.write_credential_pool("openai-codex", [row])
    assert auth.read_credential_pool("openai-codex")[0]["last_status"] == "ok"
    assert B in auth.read_credential_pool("openai-codex")[0]["model_cooldowns"]


@pytest.mark.parametrize("unsafe", ["dead", "billing", "billing_unverified", "payment", "disabled"])
def test_verified_recovery_never_clears_unsafe_observed_generation(pool, unsafe):
    from agent.credential_pool import CredentialPool, PooledCredential
    from agent.credential_pool_disabled import set_credential_disabled
    from hermes_cli import auth

    row = pool.entries()[0].to_dict()
    row.update(last_status="exhausted", last_status_at=time.time(), last_error_code=429,
               last_error_reset_at=time.time() + 3600, model_cooldowns={A: time.time() + 3600})
    if unsafe == "dead":
        row["last_status"] = "dead"
    elif unsafe == "payment":
        row["last_error_code"] = 402
    elif unsafe != "disabled":
        row["failure_reason"] = unsafe
    auth.write_credential_pool("openai-codex", [row])
    codex = CredentialPool("openai-codex", [PooledCredential.from_dict("openai-codex", row)])
    observed = codex.entries()[0]
    if unsafe == "disabled":
        set_credential_disabled("openai-codex", observed, True)
    before = auth.read_credential_pool("openai-codex")
    assert not codex.clear_verified_model_cooldown(observed, A)
    assert auth.read_credential_pool("openai-codex") == before


def test_legacy_model_bench_loses_to_external_reset(pool):
    from hermes_cli import auth
    from agent.credential_pool import load_pool

    row = pool.entries()[0].to_dict()
    row["model_cooldowns"] = {A: time.time() + 3600}
    auth.write_credential_pool("anthropic", [row])
    stale = load_pool("anthropic")
    reset = {**row, "model_cooldowns": {}, "status_cleared_at": time.time()}
    auth.write_credential_pool("anthropic", [reset], status_cleared_ids=[row["id"]])
    stale._persist()
    stale._persist()
    assert not auth.read_credential_pool("anthropic")[0]["model_cooldowns"]
    bench(stale)
    assert stale.entries()[0].model_cooldowns[A] > time.time()


def test_new_account_failure_does_not_erase_model_reset_receipt():
    from hermes_cli.auth_pool_merge import _merge_disk_cooldown_state

    memory = {"id": "seat", "status_cleared_at": 200.0, "model_cooldowns": {}}
    disk = {"id": "seat", "status_cleared_at": 100.0, "last_status": "dead", "last_status_at": 300.0}
    merged = _merge_disk_cooldown_state(memory, disk, "anthropic")
    assert merged["status_cleared_at"] == 200.0
    stale = {"id": "seat", "model_cooldowns": {A: time.time() + 3600},
             "model_cooldown_recorded_at": {A: 150.0}}
    assert not _merge_disk_cooldown_state(stale, merged, "anthropic")["model_cooldowns"]
