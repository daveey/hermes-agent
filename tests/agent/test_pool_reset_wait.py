"""A 429 against a pool whose every account has a known reset waits for that reset, not a fixed backoff.

Hub 2026-11-20: a 600 s wait woke 3 s before alex's model cooldown ended, so the turn failed. These
tests use a real ``CredentialPool`` under a sandboxed HERMES_HOME and a controlled clock.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import credential_pool as cp
from agent.model_routing_unavailable import POOL_RESET_BUDGET_S, POOL_RESET_MARGIN_S, pool_reset_wait
from agent.status_output import StatusOutputMixin
from agent.turn_recovery import compute_error_backoff

MODEL = "claude-opus-5-5"
URL = "https://api.anthropic.com"


def _pool(tmp_path, monkeypatch, resets):
    """Anthropic OAuth pool, one 429-benched account per entry of *resets* (seconds from now)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = time.time()
    entries = [
        cp.PooledCredential.from_dict("anthropic", dict(
            id=f"acct{i}", label=f"acct{i}", auth_type="oauth", priority=i, source="manual:hub_minted",
            access_token=f"tok-{i}", expires_at_ms=int((now + 86400) * 1000),
            model_cooldowns={MODEL: now + delta}))  # a model-scoped 429 leaves the credential itself ok
        for i, delta in enumerate(resets)
    ]
    return cp.CredentialPool("anthropic", entries)


def _agent(pool):
    agent = MagicMock()
    for name in ("_emit_diagnostic_wait", "_buffer_diagnostic_status"):
        setattr(agent, name, getattr(StatusOutputMixin, name).__get__(agent))
    agent._client_log_context.return_value = ""
    agent._credential_pool = pool
    return agent


def _429():
    err = Exception("HTTP 429: rate_limit_error")
    err.status_code = 429
    return err


def _backoff(agent):
    return compute_error_backoff(agent, _429(), retry_count=1, max_retries=3, is_rate_limited=True,
                                 is_zai_coding_overload=False, base_url=URL, model=MODEL)


def test_wait_runs_to_the_earliest_reset_plus_margin_and_the_credential_is_then_selected(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch, [603, 5 * 3600])
    wait = _backoff(_agent(pool))
    assert 603 <= wait <= 603 + POOL_RESET_MARGIN_S
    # After sleeping exactly that long the soonest account is selectable again (and the later one is not).
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + wait)
    assert pool.select(model=MODEL).id == "acct0"


def test_a_fixed_600s_retry_after_does_not_override_a_known_reset(tmp_path, monkeypatch):
    """The hub's failing case: Retry-After clamped to 600 s while the reset was 603 s away."""
    err = _429()
    err.response = SimpleNamespace(headers={"retry-after": "900"})
    agent = _agent(_pool(tmp_path, monkeypatch, [603]))
    wait = compute_error_backoff(agent, err, retry_count=1, max_retries=3, is_rate_limited=True,
                                 is_zai_coding_overload=False, base_url=URL, model=MODEL)
    assert 603 <= wait <= 603 + POOL_RESET_MARGIN_S


def test_a_pool_with_room_or_no_pool_keeps_the_fixed_backoff(tmp_path, monkeypatch):
    assert pool_reset_wait(_pool(tmp_path, monkeypatch, [-5]), MODEL) is None  # already reset: room
    assert pool_reset_wait(None, MODEL) is None
    assert _backoff(_agent(None)) <= 60  # jittered default, not a pool wait


def test_a_reset_past_the_cap_is_not_waited_for_in_backoff(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch, [POOL_RESET_BUDGET_S + 600])
    wait, _refusal = pool_reset_wait(pool, MODEL)
    assert wait > POOL_RESET_BUDGET_S
    assert _backoff(_agent(pool)) <= 60


def test_a_reset_past_the_cap_ends_the_turn_without_sleeping_and_names_the_reset(tmp_path, monkeypatch):
    from agent.turn_api_error import handle_api_error
    from run_agent import AIAgent

    pool = _pool(tmp_path, monkeypatch, [POOL_RESET_BUDGET_S + 600])
    reset_at = pool.entries()[0].model_cooldowns[MODEL]
    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("model_tools.check_toolset_requirements", return_value={}), \
            patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="test-key-1234567890", base_url=URL, quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent.provider, agent.model, agent._credential_pool = "anthropic", MODEL, pool
    seen = {}

    def _exhausted(agent_, api_error, classified, **kw):
        seen["error"] = api_error
        return {"final_response": str(api_error), "failed": True}

    with patch.object(agent, "_try_activate_fallback", return_value=False), \
            patch.object(agent, "_try_recover_primary_transport", return_value=False), \
            patch("agent.turn_api_error.max_retries_exhausted_result", _exhausted), \
            patch("agent.turn_api_error.compute_error_backoff", side_effect=AssertionError("must not back off")), \
            patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=AssertionError("must not sleep")), \
            patch("agent.turn_recovery_autorecover.auto_recover_after_exhaustion", return_value=None), \
            patch("agent.turn_api_error.recover_after_classification", return_value=(False, False)):
        verdict = handle_api_error(
            agent, api_error=_429(), _retry=SimpleNamespace(primary_recovery_attempted=False), thinking_spinner=None, messages=[],
            api_messages=[], api_kwargs={}, system_message="", active_system_prompt="",
            conversation_history=[], approx_tokens=0, retry_count=0, max_retries=3, compression_attempts=0,
            max_compression_attempts=1, api_call_count=1, api_request_id="r", api_start_time=time.time(),
            effective_task_id="t", turn_id="u")
    assert verdict.action == "return"
    assert str(reset_at) in seen["error"].args[0]
