"""The per-request Anthropic credential check stays on the pool account the agent is pinned to.

Regression for a hermes-hub incident: the agent ran on pool account B (``_credential_pool_entry_id``),
account A sat ahead of it in the pool with an expired access token, and the pre-request refresh
(``_try_refresh_anthropic_client_credentials``) re-resolved "the" Anthropic token as the first pool
entry, so every request swapped A's expired token back in. Recovery refreshed B and swapped it in,
the next request's pre-check swapped A back, and after two refreshes of B the turn gave up.

Real pool store under the sandboxed HERMES_HOME; only the OAuth token endpoint and the SDK client
builder are stubbed.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from run_agent import AIAgent

A_OLD = "sk-ant-oat01-account-A-expired"
B_1 = "sk-ant-oat01-account-B-gen1"
B_2 = "sk-ant-oat01-account-B-gen2"


class _StubClient:
    def __init__(self, key=None):
        self.key = key
        self.is_closed = False

    def close(self):
        self.is_closed = True


def _row(id_, token, refresh, expires_at_ms, priority):
    return {
        "id": id_, "label": id_, "auth_type": "oauth", "priority": priority, "source": "manual",
        "access_token": token, "refresh_token": refresh, "expires_at_ms": expires_at_ms,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda pid: pid == "anthropic")
    monkeypatch.setattr("agent.anthropic_credentials.read_hermes_oauth_credentials", lambda: None)
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    now_ms = int(time.time() * 1000)
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": [
        _row("acct-A", A_OLD, "rt-A", now_ms - 3_600_000, 0),   # first, expired
        _row("acct-B", B_1, "rt-B-1", now_ms + 3_600_000, 1),   # the pinned one
    ]}}))
    return hermes_home


def _pinned_agent():
    from agent.credential_pool import load_pool
    pool = load_pool("anthropic")
    entry = next(e for e in pool.entries() if e.id == "acct-B")
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.api_mode = "anthropic", "claude-opus-4-5", "anthropic_messages"
    agent.base_url = agent._anthropic_base_url = "https://api.anthropic.com"
    agent._oauth_1m_beta_disabled = False
    agent.model_routing = None
    agent._credential_pool = pool
    agent._anthropic_client = _StubClient()
    # Adopt B the way pool rotation does.
    with patch("agent.anthropic_adapter.build_anthropic_client", side_effect=lambda k, *a, **kw: _StubClient(k)):
        assert agent._swap_credential(entry) is True
    assert agent._credential_pool_entry_id == "acct-B" and agent._anthropic_api_key == B_1
    return agent


def _request_key(agent):
    with patch("agent.anthropic_adapter.build_anthropic_client", side_effect=lambda k, *a, **kw: _StubClient(k)), \
            patch.object(agent, "_force_close_tcp_sockets", return_value=0, create=True):
        client = agent._create_request_anthropic_client(reason="chat_completion_request")
    return client.key


def test_pre_request_check_keeps_the_pinned_account_over_an_expired_first_entry(home):
    agent = _pinned_agent()
    assert _request_key(agent) == B_1
    assert agent._anthropic_api_key == B_1 and agent.api_key == B_1
    assert agent._credential_pool_entry_id == "acct-B"


def test_after_recovery_refreshes_the_pinned_account_the_next_request_carries_its_new_token(home):
    agent = _pinned_agent()
    # Pool recovery after a 401: refresh B (the failing entry) and swap it in.
    refreshed = {"access_token": B_2, "refresh_token": "rt-B-2", "expires_at_ms": int(time.time() * 1000) + 3_600_000}
    with patch("agent.anthropic_credentials.refresh_anthropic_oauth_pure", return_value=refreshed) as post:
        entry = agent._credential_pool.try_refresh_matching(api_key_hint=B_1, credential_id="acct-B")
    assert entry is not None and entry.access_token == B_2 and post.call_count == 1
    with patch("agent.anthropic_adapter.build_anthropic_client", side_effect=lambda k, *a, **kw: _StubClient(k)):
        assert agent._swap_credential(entry) is True
    assert _request_key(agent) == B_2
    assert agent._credential_pool_entry_id == "acct-B"


def test_pinned_account_rotated_by_another_process_is_adopted(home):
    agent = _pinned_agent()
    data = json.loads((home / "auth.json").read_text())
    for row in data["credential_pool"]["anthropic"]:
        if row["id"] == "acct-B":
            row.update(access_token=B_2, refresh_token="rt-B-2")
    (home / "auth.json").write_text(json.dumps(data))
    assert _request_key(agent) == B_2
    assert agent._credential_pool_entry_id == "acct-B"


def test_expired_pinned_account_is_refreshed_not_swapped_for_another(home):
    from dataclasses import replace
    agent = _pinned_agent()
    pool = agent._credential_pool
    live_b = next(e for e in pool.entries() if e.id == "acct-B")
    pool._replace_entry(live_b, replace(live_b, expires_at_ms=int(time.time() * 1000) - 1000))  # B's hour ran out
    data = json.loads((home / "auth.json").read_text())
    for row in data["credential_pool"]["anthropic"]:
        if row["id"] == "acct-B":
            row["expires_at_ms"] = int(time.time() * 1000) - 1000
    (home / "auth.json").write_text(json.dumps(data))
    refreshed = {"access_token": B_2, "refresh_token": "rt-B-2", "expires_at_ms": int(time.time() * 1000) + 3_600_000}
    with patch("agent.anthropic_credentials.refresh_anthropic_oauth_pure", return_value=refreshed) as post:
        assert _request_key(agent) == B_2
    assert post.call_count == 1 and post.call_args.args[0] == "rt-B-1"
    assert agent._credential_pool_entry_id == "acct-B"


def test_model_aware_pool_resolution_skips_an_expired_entry(home):
    from agent.anthropic_credentials import _resolve_anthropic_pool_token, resolve_anthropic_token
    assert resolve_anthropic_token(model="claude-opus-4-5") == B_1
    # Diagnostics (no model) keep the read-only first-entry behaviour.
    assert _resolve_anthropic_pool_token() == A_OLD


def test_pinned_entry_removed_from_pool_rebinds_the_id_to_the_adopted_key(home, caplog):
    agent = _pinned_agent()
    agent._credential_pool._entries = [e for e in agent._credential_pool._entries if e.id != "acct-B"]
    agent._credential_pool._current_id = None
    data = json.loads((home / "auth.json").read_text())
    data["credential_pool"]["anthropic"] = [r for r in data["credential_pool"]["anthropic"] if r["id"] != "acct-B"]
    data["credential_pool"]["anthropic"][0]["expires_at_ms"] = int(time.time() * 1000) + 3_600_000
    (home / "auth.json").write_text(json.dumps(data))
    with caplog.at_level("WARNING"):
        assert _request_key(agent) == A_OLD
    assert agent._credential_pool_entry_id == "acct-A"
    assert "moved the session off pool entry acct-B" in caplog.text


def test_recovery_warns_when_the_failing_key_is_another_accounts(home, caplog):
    from agent.agent_runtime_helpers import _recover_auth_failure
    agent = _pinned_agent()
    refreshed = {"access_token": B_2, "refresh_token": "rt-B-2", "expires_at_ms": int(time.time() * 1000) + 3_600_000}
    with caplog.at_level("WARNING"), \
            patch("agent.anthropic_credentials.refresh_anthropic_oauth_pure", return_value=refreshed), \
            patch("agent.anthropic_adapter.build_anthropic_client", side_effect=lambda k, *a, **kw: _StubClient(k)):
        _recover_auth_failure(
            agent, agent._credential_pool, status_code=401, has_retried_429=False, error_context=None,
            api_key_hint=A_OLD, credential_id="acct-B", rotate_and_swap=lambda *a: False)
    assert "failing key belongs to pool entry acct-A" in caplog.text
