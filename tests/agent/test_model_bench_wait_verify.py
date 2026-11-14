"""A model-scoped 429 bench, a long retry wait, then the pre-send credential check.

Hub 2026-11-14 05:40Z (sessions 20261114_053458_725d7e, 20261114_053457_d50e20): a 429 benched the last
Anthropic subscription row for claude-sonnet-5-5 only ("other models stay available"), the turn waited
600 s with no pool entry selected, and the retry died on the non-retryable "Subscription OAuth credential
is unavailable; no selected pool entry". Real pool and runtime code; only HTTP is mocked.
"""
import json
import time
from unittest.mock import patch

import anthropic
import httpx
import pytest

from tests.agent import test_managed_subscription as fixtures
from tests.agent.test_class_failure_wire import completion

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route

MODEL = "anthropic-deep"
SIBLING_TOKEN = "sk-ant-oat01-sibling"


def _add_sibling(tmp_path, **fields):
    path = tmp_path / "auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["credential_pool"]["anthropic"].append({
        "id": "sibling", "label": "sibling", "priority": 2, "auth_type": "oauth",
        "source": "manual", "access_token": SIBLING_TOKEN, **fields})
    path.write_text(json.dumps(data), encoding="utf-8")


def _bench_runtime_for_model():
    """Resolve the live runtime, then bench its OAuth row for MODEL only, as an Anthropic 429 does."""
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODEL, routing_session_id="bench")
    pool = runtime["credential_pool"]
    assert pool.current().id == "subscription"
    assert pool.mark_exhausted_and_rotate(status_code=429, model=MODEL, credential_id="subscription",
                                          error_context={"reset_at": time.time() + 13150},
                                          failure_reason="rate_limit") is None
    assert pool.current() is None
    return runtime


def test_sole_model_bench_is_typed_quota(managed):
    from agent.model_routing_runtime import verify_class_runtime
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    runtime = _bench_runtime_for_model()
    with pytest.raises(SubscriptionQuotaUnavailable):
        verify_class_runtime(runtime, runtime["model_routing"])


def test_auth_benched_sibling_is_typed_auth_unavailable(managed, tmp_path):
    """The other OAuth row sits on a 401 bench with no cooldown of its own for this model: quota is
    unproven, but the refusal is still the typed, retryable auth wall, never the generic one."""
    from agent.model_routing_runtime import verify_class_runtime
    from agent.model_routing_unavailable import SubscriptionAuthUnavailable
    now = time.time()
    _add_sibling(tmp_path, last_status="exhausted", last_status_at=now, last_error_code=401,
                 last_error_reset_at=now + 3600, model_cooldowns={"other-model": now + 7200})
    runtime = _bench_runtime_for_model()
    with pytest.raises(SubscriptionAuthUnavailable):
        verify_class_runtime(runtime, runtime["model_routing"])


def test_sibling_freed_during_retry_wait_is_reselected(managed, tmp_path):
    """The sibling's own cooldown for this model lapses during the 600 s wait: the retry must go out
    on the sibling, not die on 'no selected pool entry'."""
    _add_sibling(tmp_path, model_cooldowns={MODEL: time.time() + 300})
    sent = []
    clock = {"skew": 0.0}
    real_time = time.time
    original = anthropic.Anthropic

    def respond(request):
        sent.append(request)
        body = json.loads(request.content)
        if len(sent) == 2:
            return httpx.Response(429, json={"type": "error", "error": {
                "type": "rate_limit_error",
                "message": "This request would exceed your account's rate limit. Please try again later.",
                "resets_at": real_time() + 13150}})
        if body.get("stream"):
            return completion(body["model"])
        return httpx.Response(200, json={"id": "message", "type": "message", "role": "assistant",
            "model": body["model"], "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})

    def sdk(**kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)
        return original(**kwargs)

    def wait_out(*_args, **_kwargs):
        clock["skew"] += 600.0

    from run_agent import AIAgent
    with patch("anthropic.Anthropic", side_effect=sdk), \
         patch("time.time", side_effect=lambda: real_time() + clock["skew"]), \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=wait_out), \
         patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=wait_out):
        agent = AIAgent(model=MODEL, provider="anthropic", session_id="bench-wait",
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        agent._disable_streaming = True
        try:
            assert agent.run_conversation("Say ok")["final_response"] == "ok"
            result = agent.run_conversation("Say ok again")
            assert result.get("final_response") == "ok", result
            assert agent._credential_pool.current().id == "sibling"
        finally:
            agent.release_clients()
    assert len(sent) == 3
    assert sent[2].headers["authorization"] == f"Bearer {SIBLING_TOKEN}"
    assert sent[1].headers["authorization"] != sent[2].headers["authorization"]
