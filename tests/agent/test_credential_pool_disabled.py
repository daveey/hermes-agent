"""User-disabled pool credentials (``hermes auth disable``) never serve, and nothing but enable undoes it."""
import json
import os
from dataclasses import replace

import pytest

from agent.credential_pool import CredentialPool, PooledCredential, load_pool
from agent import credential_pool_disabled as cpd
from hermes_cli.auth import read_credential_pool, write_credential_pool


def _rows(provider, n=3):
    return [PooledCredential(
        provider=provider, id=f"row{i}", label=f"account{i}@example.com", source="manual",
        auth_type="api_key", access_token=f"fixture-{i}", priority=i,
    ) for i in range(n)]


def _pool(provider="openrouter", n=3):
    rows = _rows(provider, n)
    write_credential_pool(provider, [e.to_dict() for e in rows])
    return CredentialPool(provider, rows)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    monkeypatch.setattr(cpd, "_root_path", lambda: None)
    cpd._cache.clear()


def _disable(provider, entry):
    assert cpd.set_credential_disabled(provider, entry, True) is True
    cpd._cache.clear()  # same-second writes can share an mtime on coarse filesystems


@pytest.mark.parametrize("strategy", ["fill_first", "round_robin", "random", "least_used"])
def test_disabled_row_is_never_selected(strategy):
    pool = _pool()
    pool._strategy = strategy
    _disable(pool.provider, pool.entries()[0])
    picks = {pool.select().id for _ in range(12)}
    assert "row0" not in picks and picks
    assert pool.peek(model="m").id != "row0"
    assert pool.reclaim("row0") is None
    assert pool.reclaim("row1") is not None


def test_all_disabled_means_no_available_entry():
    pool = _pool(n=2)
    for entry in pool.entries():
        _disable(pool.provider, entry)
    assert pool.select() is None
    assert pool.has_available() is False
    assert pool.has_credentials() is True  # kept, not removed


def test_enable_restores_selection():
    pool = _pool(n=1)
    entry = pool.entries()[0]
    _disable(pool.provider, entry)
    assert pool.select() is None
    assert cpd.set_credential_disabled(pool.provider, entry, False) is True
    cpd._cache.clear()
    assert pool.select().id == "row0"
    assert "disabled_credentials" not in _auth_json_for(pool)


def _auth_json_for(pool):
    from hermes_cli.auth import _load_auth_store
    return _load_auth_store()


def test_set_disabled_is_idempotent():
    pool = _pool(n=1)
    entry = pool.entries()[0]
    _disable(pool.provider, entry)
    assert cpd.set_credential_disabled(pool.provider, entry, True) is False
    assert cpd.set_credential_disabled(pool.provider, entry, False) is True
    assert cpd.set_credential_disabled(pool.provider, entry, False) is False


def test_stale_writer_cannot_undo_disable():
    """A pool loaded before the disable keeps persisting its rows; the disable must survive."""
    stale = _pool()
    stale.select()
    fresh = load_pool(stale.provider)
    _disable(stale.provider, next(e for e in fresh.entries() if e.id == "row1"))
    stale.reset_statuses()
    stale.mark_exhausted_and_rotate(status_code=429)  # persists every row from the stale copy
    stale._persist()
    assert cpd.is_credential_disabled(stale.provider, replace(stale.entries()[0], id="row1"))
    picks = {stale.select().id for _ in range(5)} | {load_pool(stale.provider).select().id}
    assert "row1" not in picks


def test_rotation_skips_disabled_sibling():
    pool = _pool()
    _disable(pool.provider, pool.entries()[1])
    assert pool.select().id == "row0"
    nxt = pool.mark_exhausted_and_rotate(status_code=429)
    assert nxt is not None and nxt.id == "row2"


def test_label_match_survives_recreated_row():
    pool = _pool(n=2)
    _disable(pool.provider, pool.entries()[0])
    recreated = replace(pool.entries()[0], id="newid", label="ACCOUNT0@example.com ")
    rows = [recreated.to_dict(), pool.entries()[1].to_dict()]
    write_credential_pool(pool.provider, rows, removed_ids=["row0"])
    fresh = load_pool(pool.provider)
    assert {e.id for e in fresh.entries()} == {"newid", "row1"}
    assert {fresh.select().id for _ in range(4)} == {"row1"}


def test_id_match_survives_relabel():
    pool = _pool(n=2)
    _disable(pool.provider, pool.entries()[0])
    assert cpd.is_credential_disabled(pool.provider, replace(pool.entries()[0], label="renamed"))
    assert not cpd.is_credential_disabled(pool.provider, pool.entries()[1])


def test_disable_from_another_process_reaches_live_pool(tmp_path):
    pool = _pool(n=2)
    assert pool.select().id == "row0"
    pool._current_id = None
    # Another process writes the section; the live pool sees it via the mtime-keyed cache.
    from hermes_cli.auth import _load_auth_store, _save_auth_store
    store = _load_auth_store()
    store["disabled_credentials"] = {pool.provider: [{"id": "row0", "label": ""}]}
    _save_auth_store(store)
    path = tmp_path / "hermes" / "auth.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # distinct stamp on coarse clocks
    assert pool.select().id == "row1"


def test_global_root_disable_applies_to_profile(tmp_path, monkeypatch):
    pool = _pool(n=2)
    root = tmp_path / "root-auth.json"
    root.write_text(json.dumps({"providers": {}, "disabled_credentials": {pool.provider: [{"label": "Account0@example.com"}]}}))
    monkeypatch.setattr(cpd, "_root_path", lambda: root)
    assert pool.select().id == "row1"
    assert cpd.disabled_by_root(pool.provider, pool.entries()[0])
    assert not cpd.disabled_by_root(pool.provider, pool.entries()[1])
    # Enabling in the profile does not touch the root's record.
    assert cpd.set_credential_disabled(pool.provider, pool.entries()[0], False) is False
    assert "row0" not in {pool.select().id for _ in range(3)}


def test_disabled_token_is_blocked_for_every_model():
    pool = _pool("anthropic", n=2)
    _disable("anthropic", pool.entries()[0])
    assert pool.token_is_blocked("fixture-0", model="claude-opus-5-5") is True
    assert pool.token_is_blocked("fixture-1", model="claude-opus-5-5") is False


def test_pinned_anthropic_entry_disabled_mid_session_rebinds():
    from agent.credential_pool_pin import pinned_anthropic_pool_token
    pool = _pool("anthropic", n=2)
    assert pinned_anthropic_pool_token(pool, "row0") == (True, "fixture-0")
    _disable("anthropic", pool.entries()[0])
    assert pinned_anthropic_pool_token(pool, "row0") == (False, None)
    assert pinned_anthropic_pool_token(pool, "row1") == (True, "fixture-1")


def test_disable_never_lands_on_pool_rows():
    pool = _pool(n=2)
    _disable(pool.provider, pool.entries()[0])
    assert all("disabled" not in row for row in read_credential_pool(pool.provider))
    (record,) = _auth_json_for(pool)["disabled_credentials"][pool.provider]
    assert {k: record[k] for k in ("id", "label")} == {"id": "row0", "label": "account0@example.com"}


def test_other_provider_unaffected():
    a = _pool("openrouter", n=1)
    b = _pool("anthropic", n=1)
    _disable("openrouter", a.entries()[0])
    assert a.select() is None
    assert b.select().id == "row0"


def test_unreadable_store_keeps_last_good_answer(monkeypatch, tmp_path):
    pool = _pool(n=2)
    _disable(pool.provider, pool.entries()[0])
    assert pool.select().id == "row1"
    path = tmp_path / "hermes" / "auth.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    from pathlib import Path

    real_read = Path.read_text

    def _boom(self, *a, **k):
        if self == path:
            raise OSError("EIO")
        return real_read(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", _boom)
    assert cpd.is_credential_disabled(pool.provider, pool.entries()[0])  # cached record survives
    assert cpd._file_keys(tmp_path / "missing.json", pool.provider) == cpd._EMPTY


def test_half_written_store_keeps_last_good_answer_and_never_uses_side_effecting_loader(monkeypatch, tmp_path):
    pool = _pool(n=2)
    _disable(pool.provider, pool.entries()[0])
    assert pool.select().id == "row1"
    from hermes_cli import auth as auth_mod

    def _forbidden(*_a, **_k):
        raise AssertionError("request path must not call _load_auth_store (corrupt-copy side effects)")
    monkeypatch.setattr(auth_mod, "_load_auth_store", _forbidden)
    path = tmp_path / "hermes" / "auth.json"
    good = path.read_text()
    path.write_text(good[: len(good) // 2])  # torn read: invalid JSON, new size
    assert cpd.is_credential_disabled(pool.provider, pool.entries()[0])
    assert not (tmp_path / "hermes" / "auth.json.corrupt").exists()
