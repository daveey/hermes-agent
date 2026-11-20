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


def test_profile_fallback_flush_keeps_borrowed_rows(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    prof = root / "profiles" / "coder"
    prof.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    monkeypatch.setattr(pools, "_seed_from_singletons", lambda *a: (False, set()))
    monkeypatch.setattr(pools, "_seed_from_env", lambda *a: (False, set()))
    (root / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"openrouter": [
        {"id": "g1", "label": "g", "auth_type": "api_key", "priority": 0, "source": "manual",
         "access_token": "fixture-global"}]}}))
    pool = pools.load_pool("openrouter")
    assert [e.id for e in pool.entries()] == ["g1"]
    pool._entries = [replace(e, last_status="exhausted", last_status_at=100, last_error_code=429)
                     for e in pool.entries()]
    pool._current_id = "g1"
    pool._persist()
    assert [e.id for e in pool.entries()] == ["g1"]
    assert [r["id"] for r in auth.read_credential_pool("openrouter")] == ["g1"]


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_borrowed_root_unavailable_preserves_runtime(tmp_path, monkeypatch, damage):
    import json
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(pools, "_seed_from_singletons", lambda *a: (False, set()))
    monkeypatch.setattr(pools, "_seed_from_env", lambda *a: (False, set()))
    path = root / "auth.json"
    path.write_text(json.dumps({"credential_pool": {"anthropic": [
        {"id": "g1", "source": "manual", "auth_type": "api_key", "priority": 0,
         "access_token": "fixture-global"}]}}))
    pool = pools.load_pool("anthropic")
    pool._current_id = "g1"
    if damage == "corrupt":
        path.write_text("{not json")
    else:
        path.unlink()
    for _ in range(2):
        pool._persist()
        assert [e.id for e in pool.entries()] == ["g1"]
        assert pool._current_id == "g1"
        assert "g1" in pool._persisted_token_pairs
    assert not (profile / "auth.json").exists()
    # A valid empty root pool IS evidence of deletion, unlike a missing store.
    path.write_text(json.dumps({"credential_pool": {"anthropic": []}}))
    pool._persist()
    assert pool.entries() == []
    assert pool._current_id is None


def test_peer_deletion_during_load_is_not_a_new_addition(monkeypatch):
    provider = "openrouter"
    rows = [pools.PooledCredential(provider=provider, id=f"row{i}", label=f"f{i}",
                                   source="manual", auth_type="api_key", priority=i,
                                   access_token=f"fixture-{i}") for i in range(2)]
    auth.write_credential_pool(provider, [row.to_dict() for row in rows])

    def delete_during_seed(*args):
        _remove_in_peer(provider, ["row0"])
        return False, set()

    monkeypatch.setattr(pools, "_seed_from_singletons", delete_during_seed)
    monkeypatch.setattr(pools, "_seed_from_env", lambda *a: (False, set()))
    pool = pools.load_pool(provider)
    for _ in range(2):
        pool._persist()
        assert [r["id"] for r in auth.read_credential_pool(provider)] == ["row1"]
    assert [e.id for e in pool.entries()] == ["row1"]


def test_corrupt_store_flush_self_heals_from_memory(monkeypatch):
    monkeypatch.setattr(pools, "_seed_from_singletons", lambda *a: (False, set()))
    monkeypatch.setattr(pools, "_seed_from_env", lambda *a: (False, set()))
    row = pools.PooledCredential(provider="openrouter", id="row0", label="f", source="manual",
                                 auth_type="api_key", priority=0, access_token="fixture-0")
    auth.write_credential_pool("openrouter", [row.to_dict()])
    pool = pools.load_pool("openrouter")
    auth._auth_file_path().write_text("{not json")
    pool._persist()
    assert [e.id for e in pool.entries()] == ["row0"]
    assert [r["id"] for r in auth.read_credential_pool("openrouter")] == ["row0"]
