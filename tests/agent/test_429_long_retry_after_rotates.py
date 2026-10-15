"""A first 429 whose Retry-After is long rotates the credential pool at once.

Recovery used to be "retry the same credential once, rotate on the second 429". When the
provider's Retry-After is long (Anthropic sent 600 s), that "retry once" sleeps the full
window first: a bb-worker job sat silent for 10 minutes on a throttled key while a healthy
sibling entry was available, then rotated on the second 429 and answered in 2.5 s. A wait
longer than ``LIVE_RETRY_WAIT_CAP_S`` now rotates on the first 429; a short Retry-After
keeps the retry-same-credential-once behaviour.
"""

from __future__ import annotations

import json
import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import extract_api_error_context, recover_with_credential_pool
from agent.error_classifier import FailoverReason
from run_agent import AIAgent

KEYS = ("sk-ant-api03-first-aaaaaaaaaaaaaaaa", "sk-ant-api03-second-bbbbbbbbbbbbbbbb")


class _Anthropic429(Exception):
    status_code = 429

    def __init__(self, retry_after: str):
        body = {"type": "error", "error": {"type": "rate_limit_error",
                                           "message": "This request would exceed your account's rate limit."}}
        super().__init__(f"Error code: 429 - {json.dumps(body)}")
        self.body = body
        self.response = SimpleNamespace(status_code=429, headers={"retry-after": retry_after})


@pytest.fixture
def pool(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [
        {"id": f"cred-{i}", "label": f"acct-{i}", "auth_type": "api_key", "priority": i,
         "source": "manual", "access_token": key}
        for i, key in enumerate(KEYS)
    ]}}))
    from agent.credential_pool import load_pool
    return load_pool("anthropic")


def _agent(pool):
    return types.SimpleNamespace(
        provider="anthropic", model="claude-opus-5-5", base_url="https://api.anthropic.com",
        api_key=KEYS[0], _credential_pool=pool, _credential_pool_entry_id="cred-0",
        _swap_credential=MagicMock(return_value=True),
    )


def _recover(agent, retry_after: str):
    return recover_with_credential_pool(
        agent, status_code=429, has_retried_429=False, classified_reason=FailoverReason.rate_limit,
        error_context=extract_api_error_context(_Anthropic429(retry_after)),
    )


def test_first_429_with_long_retry_after_rotates_to_sibling(pool):
    assert pool.select().id == "cred-0"
    agent = _agent(pool)

    assert _recover(agent, "600") == (True, False)

    agent._swap_credential.assert_called_once()
    assert agent._swap_credential.call_args.args[0].id == "cred-1"


def test_first_429_with_short_retry_after_still_retries_same_credential(pool):
    pool.select()
    agent = _agent(pool)

    assert _recover(agent, "5") == (False, True)

    agent._swap_credential.assert_not_called()
    assert all(e.last_status is None and not e.model_cooldowns for e in pool.entries())


def test_long_retry_after_on_a_sole_credential_does_not_report_recovery(tmp_path, monkeypatch, pool):
    # Nothing to rotate to: no recovery, so the caller falls back (or waits) as before.
    pool._entries = [pool.entries()[0]]
    pool.select()
    agent = _agent(pool)

    recovered, has_retried = _recover(agent, "600")

    assert recovered is False and has_retried is True
    agent._swap_credential.assert_not_called()


# ---------------------------------------------------------------------------
# Full loop: the turn answers from the second entry without any backoff sleep.
# ---------------------------------------------------------------------------

def _tool_defs():
    return [{"type": "function", "function": {
        "name": "web_search", "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _ok(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="claude-opus-5-5", usage=None)


def test_run_conversation_rotates_without_sleeping_retry_after(pool):
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key=KEYS[0], base_url="https://openrouter.ai/api/v1", provider="openrouter",
            model="anthropic/claude-opus-5-5", quiet_mode=True, skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    # Pool-bound session on the first entry; provider/base_url aligned with the pool so the
    # provider-boundary guard lets recovery act on it.
    agent.provider = "anthropic"
    agent._credential_pool = pool
    pool.select()
    agent._credential_pool_entry_id = "cred-0"

    used_keys: list[str] = []

    def fake_api_call(api_kwargs):
        used_keys.append(agent.api_key)
        if len(used_keys) == 1:
            raise _Anthropic429("600")
        return _ok("answered")

    def fake_swap(entry):
        agent.api_key = entry.runtime_api_key
        agent._credential_pool_entry_id = entry.id
        return True

    waits: list[float] = []

    def fake_sleep(_agent, wait_time, *a, **k):
        waits.append(wait_time)

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_swap_credential", side_effect=fake_swap),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=fake_sleep),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("what's on the triptic?")

    assert result["final_response"] == "answered"
    assert used_keys == [KEYS[0], KEYS[1]]
    assert not any(w >= 60 for w in waits), waits
