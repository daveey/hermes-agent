"""Relay policy has independent ownership and applies only to pulled rows."""
from dataclasses import replace

import pytest

from agent import credential_pool_disabled as cpd
from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli import auth


@pytest.fixture
def row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cpd, "_root_path", lambda: None)
    return PooledCredential(provider="anthropic", id="pulled", label="device_code",
                            source="manual:hub_minted", access_token="fixture", auth_type="oauth", priority=0)


def seed(row):
    record = {"label": row.label, "source": "hub-oauth-relay", "scope": "manual:hub_minted"}
    store = auth._load_auth_store()
    store["disabled_credentials"] = {"anthropic": [record]}
    auth._save_auth_store(store)
    return record


@pytest.mark.parametrize("label", ["device_code", "account@example.com"])
def test_relay_matches_generic_transport_label_but_not_own_grant(row, label):
    row = replace(row, label=label)
    seed(row)
    assert cpd.is_credential_disabled("anthropic", row)
    assert not cpd.is_credential_disabled("anthropic", replace(row, source="manual"))
    assert not cpd.is_credential_disabled("anthropic", replace(row, label="different"))


def test_local_disable_after_relay_is_independent(row):
    row = replace(row, label="account@example.com")
    remote = seed(row)
    assert cpd.set_credential_disabled("anthropic", row, True)
    store = auth._load_auth_store()
    records = store["disabled_credentials"]["anthropic"]
    assert remote in records and len(records) == 2
    store["disabled_credentials"]["anthropic"] = [r for r in records if r != remote]
    auth._save_auth_store(store)
    assert CredentialPool("anthropic", [row]).select() is None


@pytest.mark.parametrize("operation", ["enable", "remove"])
def test_local_operations_never_remove_relay_policy(row, operation):
    row = replace(row, label="account@example.com")
    remote = seed(row)
    if operation == "enable":
        assert not cpd.set_credential_disabled("anthropic", row, False)
    else:
        assert not cpd.forget_removed("anthropic", row, [])
    assert auth._load_auth_store()["disabled_credentials"]["anthropic"] == [remote]
    assert cpd.is_credential_disabled("anthropic", row)


def test_local_enable_removes_local_record_only(row):
    row = replace(row, label="account@example.com")
    cpd.set_credential_disabled("anthropic", row, True)
    store = auth._load_auth_store()
    remote = {"label": row.label, "source": "hub-oauth-relay", "scope": "manual:hub_minted"}
    store["disabled_credentials"]["anthropic"].append(remote)
    auth._save_auth_store(store)
    assert cpd.set_credential_disabled("anthropic", row, False)
    assert auth._load_auth_store()["disabled_credentials"]["anthropic"] == [remote]
