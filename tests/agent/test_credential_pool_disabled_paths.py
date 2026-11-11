"""Paths that hand out a token without pool selection also refuse a `hermes auth disable`d account."""
import os
from types import SimpleNamespace

import pytest

from agent import credential_pool_disabled as cpd
from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli.auth import AuthError, write_credential_pool


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    monkeypatch.setattr(cpd, "_root_path", lambda: None)
    cpd._cache.clear()


def _pool(provider, n=2):
    rows = [PooledCredential(provider=provider, id=f"row{i}", label=f"account{i}@example.com", source="manual",
                             auth_type="api_key", access_token=f"fixture-{provider}-{i}", priority=i)
            for i in range(n)]
    write_credential_pool(provider, [e.to_dict() for e in rows])
    return CredentialPool(provider, rows)


def _disable(provider, entry):
    assert cpd.set_credential_disabled(provider, entry, True) is True
    cpd._cache.clear()


def test_token_disabled_matches_only_disabled_rows_and_is_free_when_nothing_disabled(monkeypatch):
    pool = _pool("openai-codex")
    import agent.credential_pool as cp
    real, loads = cp.load_pool, []
    monkeypatch.setattr(cp, "load_pool", lambda p: loads.append(p) or real(p))
    assert cpd.token_disabled("openai-codex", "fixture-openai-codex-0") is False
    assert loads == []  # nothing disabled: no pool load on the request path
    _disable("openai-codex", pool.entries()[0])
    assert cpd.token_disabled("openai-codex", "fixture-openai-codex-0") is True
    assert cpd.token_disabled("openai-codex", "fixture-openai-codex-1") is False
    assert cpd.token_disabled("openai-codex", "unknown") is False


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
def test_singleton_runtime_of_a_disabled_account_raises(monkeypatch, provider):
    from hermes_cli import runtime_provider as rp
    pool = _pool(provider)
    _disable(provider, pool.entries()[0])
    name = "resolve_codex_runtime_credentials" if provider == "openai-codex" else "resolve_xai_oauth_runtime_credentials"
    monkeypatch.setattr(rp, name, lambda: {"api_key": f"fixture-{provider}-0", "base_url": "https://x.example"})
    with pytest.raises(AuthError, match="disabled"):
        rp._resolve_oauth_runtime(provider, provider, {}, None)
    monkeypatch.setattr(rp, name, lambda: {"api_key": f"fixture-{provider}-1", "base_url": "https://x.example"})
    assert rp._resolve_oauth_runtime(provider, provider, {}, None)["api_key"] == f"fixture-{provider}-1"


def test_auxiliary_codex_singleton_token_of_a_disabled_account_is_dropped(monkeypatch):
    from hermes_cli import auth
    from agent import auxiliary_client as aux
    pool = _pool("openai-codex")
    monkeypatch.setattr(auth, "_read_codex_tokens", lambda: {"tokens": {"access_token": "fixture-openai-codex-0"}})
    assert aux._read_codex_singleton_token() == "fixture-openai-codex-0"
    _disable("openai-codex", pool.entries()[0])
    assert aux._read_codex_singleton_token() is None


def test_anthropic_token_of_a_disabled_account_is_dropped_even_without_a_model():
    from agent.anthropic_credentials import _available_anthropic_token
    pool = _pool("anthropic")
    assert _available_anthropic_token("fixture-anthropic-0", None) == "fixture-anthropic-0"
    _disable("anthropic", pool.entries()[0])
    assert _available_anthropic_token("fixture-anthropic-0", None) is None
    assert _available_anthropic_token("fixture-anthropic-0", "claude-x") is None
    assert _available_anthropic_token("fixture-anthropic-1", None) == "fixture-anthropic-1"


class _Agent(SimpleNamespace):
    def _adopt_openai_credentials(self, api_key, base_url, *, reason):
        self.adopted.append((api_key, base_url, reason))
        self.api_key = api_key
        return True


def _agent(pool, entry_id="row0"):
    return _Agent(provider=pool.provider, _credential_pool=pool, _credential_pool_entry_id=entry_id,
                  api_key=f"fixture-{pool.provider}-0", base_url="https://chatgpt.example/backend", adopted=[])


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
def test_bound_session_moves_off_a_disabled_row_before_the_next_request(provider):
    pool = _pool(provider)
    agent = _agent(pool)
    cpd.rebind_off_disabled(agent)
    assert agent.adopted == []  # nothing disabled: untouched
    _disable(provider, pool.entries()[0])
    cpd.rebind_off_disabled(agent)
    assert agent.adopted == [(f"fixture-{provider}-1", "https://chatgpt.example/backend", "credential_disabled")]
    assert agent._credential_pool_entry_id == "row1"
    _disable(provider, pool.entries()[1])
    with pytest.raises(AuthError, match="Every .* account is disabled"):
        cpd.rebind_off_disabled(agent)


def test_unattributed_session_is_matched_by_its_token():
    pool = _pool("openai-codex")
    agent = _agent(pool, entry_id=None)
    _disable("openai-codex", pool.entries()[0])
    cpd.rebind_off_disabled(agent)
    assert agent._credential_pool_entry_id == "row1"


def test_request_client_checkout_runs_the_rebind(monkeypatch):
    from agent.client_lifecycle import ClientLifecycleMixin

    class Stop(Exception):
        pass

    def _rebind(agent):
        raise Stop
    monkeypatch.setattr(cpd, "rebind_off_disabled", _rebind)
    with pytest.raises(Stop):
        ClientLifecycleMixin._create_request_openai_client(SimpleNamespace(), reason="test")


def test_refresh_and_lease_refuse_a_disabled_row(monkeypatch):
    pool = _pool("openrouter")
    pool._current_id = "row0"
    _disable("openrouter", pool.entries()[0])
    monkeypatch.setattr(pool, "_refresh_entry", lambda *a, **k: pytest.fail("refreshed a disabled row"))
    assert pool.try_refresh_current() is None
    assert pool.try_refresh_matching(credential_id="row0") is None
    assert pool.acquire_lease("row0") == "row1"


def test_cache_sees_a_same_size_same_mtime_replace(tmp_path):
    pool = _pool("openrouter")
    _disable("openrouter", pool.entries()[0])
    path = tmp_path / "hermes" / "auth.json"
    assert cpd.is_credential_disabled("openrouter", pool.entries()[0])
    st = path.stat()
    swapped = path.read_text().replace('"row0"', '"row1"').replace("account0@", "account1@")
    tmp = path.with_suffix(".new")
    tmp.write_text(swapped)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, path)  # same size and mtime, new inode (the atomic save)
    assert cpd.is_credential_disabled("openrouter", pool.entries()[1])
    assert not cpd.is_credential_disabled("openrouter", pool.entries()[0])


def test_anthropic_session_pinned_to_its_only_row_stops_when_that_row_is_disabled(monkeypatch):
    """Reviewer repro: pin check reports the row gone, re-resolve finds nothing, the old token must not go out."""
    from agent.client_lifecycle import ClientLifecycleMixin
    for key in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    pool = _pool("anthropic", n=1)
    agent = ClientLifecycleMixin.__new__(type("A", (ClientLifecycleMixin,), {}))
    agent.api_mode, agent.provider, agent.model, agent.model_routing = "anthropic_messages", "anthropic", "claude-x", None
    agent._anthropic_api_key = agent.api_key = "fixture-anthropic-0"
    agent._anthropic_base_url, agent._is_anthropic_oauth = "", False
    agent._credential_pool, agent._credential_pool_entry_id = pool, "row0"
    import agent.anthropic_credentials as ac
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: None)
    assert agent._try_refresh_anthropic_client_credentials() is False  # enabled: nothing to change
    _disable("anthropic", pool.entries()[0])
    with pytest.raises(AuthError, match="Every anthropic account is disabled"):
        agent._try_refresh_anthropic_client_credentials()
