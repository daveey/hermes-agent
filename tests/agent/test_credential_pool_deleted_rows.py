"""A stale runtime must not resurrect rows removed by another auth process."""
from dataclasses import replace
import multiprocessing

import pytest

from agent import credential_pool as pools
from hermes_cli import auth


def _remove_in_peer(provider, ids):
    # The production writer reads/merges/saves under _auth_store_lock.
    auth.write_credential_pool(provider, [], removed_ids=ids)


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
@pytest.mark.parametrize("remove_all", [False, True])
def test_stale_runtime_flush_after_peer_removal(monkeypatch, provider, remove_all):
    monkeypatch.setattr(pools, "_seed_from_singletons", lambda *args: (False, set()))
    monkeypatch.setattr(pools, "_seed_from_env", lambda *args: (False, set()))
    rows = [pools.PooledCredential(
        provider=provider, id=f"row{i}", label=f"fixture{i}", source="manual",
        auth_type="api_key", priority=i, access_token=f"fixture-{i}",
    ) for i in range(2)]
    auth.write_credential_pool(provider, [e.to_dict() for e in rows])
    stale = pools.load_pool(provider)
    stale._current_id = "row0"
    removed = [e.id for e in rows] if remove_all else ["row0"]
    peer = multiprocessing.get_context("fork").Process(target=_remove_in_peer, args=(provider, removed))
    peer.start()
    peer.join(10)
    try:
        assert peer.exitcode == 0
    finally:
        if peer.is_alive():
            peer.terminate()
            peer.join(10)
    assert not (set(auth._entry_ids(auth.read_credential_pool(provider))) & set(removed))
    # A new login by a third writer must survive the stale runtime's flush too.
    fresh = replace(rows[0], id="fresh", access_token="fixture-fresh")
    auth.write_credential_pool(provider, [fresh.to_dict()])
    stale._entries = [replace(e, last_status="exhausted", last_status_at=100,
                              last_error_code=429) for e in stale.entries()]
    for _ in range(2):
        stale._persist()
        written = auth._entry_ids(auth.read_credential_pool(provider))
        assert not (set(written) & set(removed))
        assert written["fresh"]["access_token"] == "fixture-fresh"
        if not remove_all:
            assert written["row1"]["last_status"] == "exhausted"
    assert not (set(e.id for e in stale.entries()) & set(removed))
    assert stale._current_id is None
    # Explicit addition is still supported after reconciliation.
    stale.add_entry(replace(rows[0], id="explicit"))
    assert "explicit" in auth._entry_ids(auth.read_credential_pool(provider))


def test_tokenless_known_row_cannot_be_reinserted():
    row = {"id": "blank", "source": "manual", "auth_type": "api_key"}
    auth.write_credential_pool("openrouter", [row])
    bases = auth._token_pairs_by_id([row])
    auth.write_credential_pool("openrouter", [], removed_ids=["blank"])
    assert auth.write_credential_pool("openrouter", [row], token_bases=bases) == []
