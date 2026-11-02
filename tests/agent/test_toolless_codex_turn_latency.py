"""[daveey] Toolless Codex turns end on the first final text, and an exhausted pool fails over at once.

bb-aux (API server, ``platform_toolsets.api_server: []``) saw 20 of 878 toolless turns make 2-4 model
calls: gpt-6-luna ended a ``completed`` response on a ``phase=commentary`` message, the normalizer
marked it ``incomplete`` (commentary never ends a tool-using turn) and the loop re-called the model.
With no tools offered there is no tool call to wait for, so the commentary is the answer.

A class-routed agent has no legacy ``_fallback_chain``, so the eager rate-limit fallback never fired
for it: an exhausted codex pool waited out the backoff and a second attempt before same-class failover.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.error_classifier import FailoverReason
from agent.transports.codex import ResponsesApiTransport
from agent.turn_response_intake import promote_toolless_codex_commentary


def _commentary_response(*, status="completed", item_status="completed", final=False):
    output = [
        SimpleNamespace(type="reasoning", id="rs_1", summary=[], encrypted_content="enc", status=None),
        SimpleNamespace(
            type="message", role="assistant", status=item_status, phase="commentary", id="msg_c",
            content=[SimpleNamespace(type="output_text", text="Sure, the lights are off now.")],
        ),
    ]
    if final:
        output.append(SimpleNamespace(
            type="message", role="assistant", status="completed", phase="final_answer", id="msg_f",
            content=[SimpleNamespace(type="output_text", text="Done.")],
        ))
    return SimpleNamespace(status=status, output=output, output_text="", incomplete_details=None)


def _agent(tools=()):
    return SimpleNamespace(valid_tool_names=set(tools), log_prefix="")


def _normalized(response):
    return ResponsesApiTransport().normalize_response(response, issuer_kind="codex_backend")


def test_commentary_only_completed_response_is_incomplete_before_promotion():
    # Guards the premise: the normalizer alone sends this turn round the continuation loop.
    msg = _normalized(_commentary_response())
    assert msg.finish_reason == "incomplete"
    assert not (msg.content or "").strip()


def test_toolless_agent_promotes_commentary_to_final_answer(caplog):
    response = _commentary_response()
    msg = _normalized(response)
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        assert promote_toolless_codex_commentary(_agent(), msg, response) is True
    assert msg.finish_reason == "stop"
    assert msg.content == "Sure, the lights are off now."
    assert "Sure, the lights" not in (msg.reasoning or "")
    # The replay sidecar is untouched: the wire history still says what the model sent.
    assert [i["phase"] for i in msg.codex_message_items] == ["commentary"]
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "ended on commentary with no tools offered" in log
    assert "lights" not in log  # no message text in agent.log


@pytest.mark.parametrize("case", ["tools_offered", "response_incomplete", "item_in_progress"])
def test_other_incomplete_responses_keep_continuing(case):
    response = _commentary_response(
        status="incomplete" if case == "response_incomplete" else "completed",
        item_status="in_progress" if case == "item_in_progress" else "completed",
    )
    msg = _normalized(response)
    agent = _agent(tools=("terminal",) if case == "tools_offered" else ())
    assert promote_toolless_codex_commentary(agent, msg, response) is False
    assert msg.finish_reason == "incomplete"
    assert not (msg.content or "").strip()


def test_reasoning_only_response_is_not_promoted():
    response = SimpleNamespace(
        status="completed", output_text="", incomplete_details=None,
        output=[SimpleNamespace(type="reasoning", id="rs_1", summary=[], encrypted_content="enc", status=None)],
    )
    msg = _normalized(response)
    assert msg.finish_reason == "incomplete"
    assert promote_toolless_codex_commentary(_agent(), msg, response) is False


def test_commentary_plus_final_answer_is_already_final():
    msg = _normalized(_commentary_response(final=True))
    assert msg.finish_reason == "stop"
    assert msg.content == "Done."


def test_continuation_logs_its_reason_without_text(caplog):
    from agent.turn_truncation import continue_codex_incomplete

    response = _commentary_response()
    msg = _normalized(response)
    agent = MagicMock()
    agent.quiet_mode, agent.log_prefix, agent.max_tokens = True, "", 2000
    agent.valid_tool_names = {"terminal"}
    agent._codex_incomplete_retries = 0
    agent._codex_reasoning_only_streak = 0
    agent._build_assistant_message.side_effect = lambda m, fr: {
        "role": "assistant", "content": m.content or "", "finish_reason": fr,
        "codex_reasoning_items": m.codex_reasoning_items, "codex_message_items": m.codex_message_items,
    }
    agent._interim_assistant_visible_text.return_value = ""
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        continue_codex_incomplete(agent, msg, "incomplete", messages=[{"role": "user", "content": "x"}],
                                  conversation_history=None, api_call_count=1, response=response)
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "Codex continuation 1/3: reason=commentary_only response_status=completed tools_offered=True" in log
    assert "lights" not in log


class _RateLimited(RuntimeError):
    status_code = 429


def _route(agent, monkeypatch):
    import agent.conversation_loop as cl
    from agent.turn_recovery import route_classified_error

    monkeypatch.setattr(cl, "_arm_fallback_restart", lambda a, m, p, r: p)
    monkeypatch.setattr(cl, "_ra", lambda: SimpleNamespace(
        _pool_may_recover_from_rate_limit=lambda pool: False))
    classified = SimpleNamespace(reason=FailoverReason.rate_limit, error_context={}, is_auth=False,
                                 should_fallback=True, retryable=True)
    err = _RateLimited("HTTP 429: The usage limit has been reached")
    return route_classified_error(
        agent, err, classified, MagicMock(), error_msg=str(err), error_context={},
        recovered_with_pool=False, base_url="https://chatgpt.com/backend-api/codex", model="gpt-6-luna",
        messages=[], api_messages=[], system_message="", active_system_prompt="", conversation_history=None,
        retry_count=1, max_retries=3, compression_attempts=0, max_compression_attempts=3, api_call_count=1,
        effective_task_id=None,
    )


def _class_agent(routing):
    agent = MagicMock()
    agent.provider, agent.model = "openai-codex", "gpt-6-luna"
    agent._fallback_index, agent._fallback_chain = 0, []
    agent.model_routing = routing
    agent._try_activate_fallback.return_value = True
    return agent


def test_exhausted_pool_on_class_route_fails_over_without_backoff(monkeypatch):
    agent = _class_agent({"requested_class": "fast"})
    verdict = _route(agent, monkeypatch)
    assert verdict.action == "break"
    agent._try_activate_fallback.assert_called_once()
    assert agent._try_activate_fallback.call_args.kwargs["reason"] == FailoverReason.rate_limit


@pytest.mark.parametrize("routing", [None, {"requested_class": "fast", "exact_pin": True}])
def test_unrouted_or_pinned_agent_keeps_legacy_gate(monkeypatch, routing):
    agent = _class_agent(routing)
    verdict = _route(agent, monkeypatch)
    assert verdict.action != "break"
    agent._try_activate_fallback.assert_not_called()


def test_turn_intake_ends_toolless_commentary_turn_without_continuing(monkeypatch):
    """The call site: the intake hands the turn to the text-response path, not the Codex continuation."""
    import agent.turn_response_intake as intake
    import hermes_cli.observability.shared_metrics_harness as metrics

    monkeypatch.setattr(intake, "splice_provider_projection", lambda *a, **k: None)
    monkeypatch.setattr(intake, "_fire_post_api_request_hook", lambda *a, **k: None)
    monkeypatch.setattr(metrics, "record_reply_content", lambda *a, **k: None)
    continued = []
    monkeypatch.setattr(intake, "continue_codex_incomplete", lambda *a, **k: continued.append(1))
    transport = ResponsesApiTransport()
    transport._last_issuer_kind = "codex_backend"
    agent = MagicMock()
    agent.api_mode, agent.valid_tool_names, agent.log_prefix, agent.quiet_mode = "codex_responses", set(), "", True
    agent.tool_progress_callback = None
    agent._final_gate_buffer = None
    agent._get_transport.return_value = transport
    agent._incomplete_scratchpad_retries = 0
    verdict = intake.normalize_model_response(
        agent, response=_commentary_response(), messages=[], api_messages=[], conversation_history=None,
        api_call_count=1, api_duration=1.0, api_start_time=0.0, api_request_id="r", effective_task_id=None,
        turn_id="t",
    )
    assert continued == []
    assert verdict.action == "fallthrough"
    assert verdict.finish_reason == "stop"
    assert verdict.assistant_message.content == "Sure, the lights are off now."
