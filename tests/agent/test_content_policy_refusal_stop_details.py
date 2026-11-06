"""handle_content_policy_refusal: an Anthropic refusal with an empty body still tells the user why.

[daveey] The refused turn leaves the MODEL's context (upstream) and is retained for display only as a
``content_refused`` row. These tests pin that the fork's validated refusal category and the
possible-side-effect warning reach that real display boundary (the stored notice) and the live result,
end to end from the production refusal handler, while nothing refused re-enters model history.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.conversation_loop import _close_durable_failed_turn
from agent.managed_worker import accept_result, result_frame
from agent.turn_failure_copy import (
    CONTENT_REFUSED_DISPLAY_KIND, CONTENT_REFUSED_METADATA_KEY, CONTENT_REFUSED_NOTICE, FAILED_TURN_NOTICE,
    REFUSED_TURN_ACTIONS_WARNING, refused_turn_display_notice,
)
from agent.turn_retry_state import TurnRetryState
from agent.turn_truncation import handle_content_policy_refusal
from hermes_state import SessionDB
from hermes_state_rewind import retain_refused_turn_for_display

_ASK = "Why choose this host?"
_TOOL_CALL = {"id": "call", "type": "function", "function": {"name": "shell", "arguments": "{}"}}


def _agent():
    import agent.transports.anthropic
    from agent.transports import get_transport

    agent = MagicMock()
    agent.api_mode, agent.provider, agent.model, agent.log_prefix = "anthropic_messages", "anthropic", "claude", ""
    agent._is_anthropic_oauth = False
    agent._get_transport.return_value = get_transport("anthropic_messages")
    agent._has_pending_fallback.return_value = False
    agent._try_activate_fallback.return_value = False
    agent._extract_reasoning.return_value = ""
    agent._persist_disabled = False
    agent._failed_turn_notice_override = None
    agent.session_id = "refusal-test"
    return agent


def _refuse(agent, messages, stop_details, *, current_turn_user_idx=None):
    response = SimpleNamespace(content=[], stop_reason="refusal", usage=None, model="claude",
                               stop_details=stop_details)
    return handle_content_policy_refusal(
        agent, response, TurnRetryState(), thinking_spinner=None, messages=messages,
        api_messages=[], api_kwargs={}, active_system_prompt=None, conversation_history=[],
        api_call_count=1, effective_task_id="t", turn_id="u", api_request_id="r",
        api_start_time=0.0, retry_count=0, max_retries=0, current_turn_user_idx=current_turn_user_idx,
    )


def _db_with_refused_turn(tmp_path, partial):
    """A real state.db holding a prior exchange plus the refused turn the agent flushed at turn start;
    returns the db and the loop's working set (turn rows stamped as committed, as the flush does)."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", "cli")
    db.append_message("s", "user", "hello")
    db.append_message("s", "assistant", "hi there")
    rows = [("user", _ASK, {})]
    if partial:
        rows += [("assistant", "", {"tool_calls": [_TOOL_CALL]}), ("tool", "done", {"tool_call_id": "call"})]
    working = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]
    for role, content, extra in rows:
        row_id = db.append_message("s", role, content, **extra)
        working.append({"role": role, "content": content, **extra, "_row_id": row_id, _DB_PERSISTED_MARKER: True})
    return db, working


def _model(db):
    return [(m["role"], m["content"]) for m in db.get_messages_as_conversation("s") if m["role"] != "session_meta"]


def _display(db):
    return [m for m in db.get_messages_as_conversation("s", include_compacted=True) if m["role"] != "session_meta"]


def test_empty_refusal_reports_stop_details_explanation():
    verdict = _refuse(_agent(), [], {"type": "refusal", "category": "general_harms", "explanation": "classifier halt"})
    assert verdict.action == "return"
    assert "classifier halt" in verdict.result["error"]


@pytest.mark.parametrize("explanation", [None, "classifier halt"])
@pytest.mark.parametrize("partial", [False, True])
def test_refusal_category_and_actions_reach_the_retained_display_notice(tmp_path, explanation, partial):
    """Production path: refusal handler → (no-op) failed-turn closer → owner retention in a real db."""
    agent = _agent()
    db, working = _db_with_refused_turn(tmp_path, partial)
    agent._session_db = db
    try:
        verdict = _refuse(agent, working, {"category": "reasoning_extraction", "explanation": explanation},
                          current_turn_user_idx=2)
        result = verdict.result
        # Upstream: the refused turn never re-enters model history (CLI/ACP/TUI keep result["messages"]).
        assert result["messages"] == working[:2]
        # The core closer adds no failed_turn boundary for a refusal (it would only be rewound).
        _close_durable_failed_turn(agent, result)
        assert result["messages"] == working[:2]
        assert not any(m.get("display_kind") == "failed_turn" for m in _display(db))
        # Live result: provider guidance, plus the actions warning iff a tool ran.
        assert "safety filter refused" in result["final_response"]
        assert "left out of this conversation" in result["final_response"]
        assert (REFUSED_TURN_ACTIONS_WARNING in result["final_response"]) == partial
        assert result["refused_turn_ran_actions"] is partial
        assert "(category: reasoning_extraction)" in result["refused_turn_notice"]
        assert (REFUSED_TURN_ACTIONS_WARNING in result["refused_turn_notice"]) == partial

        # Real display boundary: the owner retains the turn with the notice built from the result.
        assert retain_refused_turn_for_display(
            db, "s", notice=refused_turn_display_notice(result),
            boundary_row_id=result["refused_turn_boundary_row_id"]) is True
        assert _model(db) == [("user", "hello"), ("assistant", "hi there")]
        refused = _display(db)[-1]
        assert (refused["role"], refused["content"], refused["display_kind"]) == (
            "user", _ASK, CONTENT_REFUSED_DISPLAY_KIND)
        notice = refused["display_metadata"][CONTENT_REFUSED_METADATA_KEY]["notice"]
        assert notice == result["refused_turn_notice"]
        assert "refused by the model's safety filter" in notice and "reasoning_extraction" in notice
        assert "Your request was not processed" not in notice
        assert ("Some actions may already have run" in notice) == partial
        # Display-only: neither the notice nor the refused ask is in the model read.
        assert all(notice not in str(c) and _ASK != c for _, c in _model(db))
    finally:
        db.close()


@pytest.mark.parametrize("category", [None, {}, "", "x" * 81, "<script>bad</script>", "secret\ntext", "naïve"])
def test_missing_or_malformed_category_never_reaches_the_display(tmp_path, category):
    """Production path control: a malformed provider category is dropped from both the live notice
    and the stored ``content_refused`` notice, which fall back to upstream's exact copy."""
    agent = _agent()
    db, working = _db_with_refused_turn(tmp_path, partial=False)
    agent._session_db = db
    try:
        result = _refuse(agent, working, {"category": category}, current_turn_user_idx=2).result
        assert result["refused_turn_notice"] == CONTENT_REFUSED_NOTICE
        assert retain_refused_turn_for_display(
            db, "s", notice=refused_turn_display_notice(result),
            boundary_row_id=result["refused_turn_boundary_row_id"]) is True
        notice = _display(db)[-1]["display_metadata"][CONTENT_REFUSED_METADATA_KEY]["notice"]
        assert notice == CONTENT_REFUSED_NOTICE
        for leaked in ("category:", "<script>", "secret", "naïve", "x" * 81):
            assert leaked not in notice
    finally:
        db.close()


def test_owner_rebuilds_the_notice_instead_of_trusting_result_text():
    """A tampered/crossed-boundary result cannot inject text: the owner re-validates the typed fields."""
    tampered = {"failure_reason": "content_policy_blocked", "refusal_category": "<b>x</b>",
                "refused_turn_ran_actions": "yes", "refused_turn_notice": "<script>pwn</script>"}
    assert refused_turn_display_notice(tampered) == CONTENT_REFUSED_NOTICE
    assert refused_turn_display_notice(None) == CONTENT_REFUSED_NOTICE


@pytest.mark.parametrize("partial", [False, True])
def test_managed_worker_frame_carries_category_and_actions_to_the_owner_notice(tmp_path, partial):
    """Bot Chat: the worker's result crosses ``result_frame``/``accept_result``; the owner's gateway
    cleanup (``_finalize_managed_refusal``) must still store the category and the warning."""
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore, SessionSource, SessionStore
    from gateway.session_managed_worker import _finalize_managed_refusal

    worker_messages = [{"role": "user", "content": _ASK}]
    if partial:
        worker_messages += [{"role": "assistant", "content": "", "tool_calls": [_TOOL_CALL]},
                            {"role": "tool", "content": "done", "tool_call_id": "call"}]
    result = _refuse(_agent(), worker_messages, {"category": "general_harms"}, current_turn_user_idx=0).result
    accepted, _usage = accept_result(result_frame(result))
    assert accepted["refusal_category"] == "general_harms"
    assert accepted["refused_turn_ran_actions"] is partial

    async def check():
        store = SessionStore(tmp_path / "sessions", GatewayConfig())
        runner = object.__new__(GatewayRunner)
        runner.session_store = store
        runner._async_session_store = AsyncSessionStore(store)
        runner._session_db = object()
        runner._evict_cached_agent = lambda *a, **k: None
        entry = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="u"))
        sid = entry.session_id
        db = store._db_for_session_id(sid)
        db.append_message(sid, "user", "hello")
        db.append_message(sid, "assistant", "hi there")
        boundary = db.append_message(sid, "user", _ASK)
        if partial:
            db.append_message(sid, "assistant", "", tool_calls=[_TOOL_CALL])
            db.append_message(sid, "tool", "done", tool_call_id="call")
        await _finalize_managed_refusal(SimpleNamespace(runner=runner), sid,
                                        {**accepted, "refused_turn_boundary_row_id": boundary})
        model = [(m["role"], m["content"]) for m in db.get_messages_as_conversation(sid)
                 if m["role"] != "session_meta"]
        assert model == [("user", "hello"), ("assistant", "hi there")]
        refused = [m for m in db.get_messages_as_conversation(sid, include_compacted=True)
                   if m["role"] != "session_meta"][-1]
        assert refused["display_kind"] == CONTENT_REFUSED_DISPLAY_KIND
        notice = refused["display_metadata"][CONTENT_REFUSED_METADATA_KEY]["notice"]
        assert "(category: general_harms)" in notice
        assert (REFUSED_TURN_ACTIONS_WARNING in notice) == partial
        db.close()

    asyncio.run(check())


def test_managed_worker_frame_drops_a_non_string_category():
    result = _refuse(_agent(), [{"role": "user", "content": _ASK}], {"category": {"nested": "x"}},
                     current_turn_user_idx=0).result
    accepted, _usage = accept_result(result_frame(result))
    assert "refusal_category" not in accepted
    assert refused_turn_display_notice(accepted) == CONTENT_REFUSED_NOTICE


def test_other_failure_keeps_generic_visible_boundary():
    agent = _agent()
    agent.session_id = "failure-test"
    agent._session_db.latest_conversation_role.return_value = "user"
    messages = [{"role": "user", "content": "Hello"}]
    _close_durable_failed_turn(agent, {
        "messages": messages, "completed": False, "failed": True, "failure_reason": "server_error",
    })
    assert messages[-1]["display_kind"] == "failed_turn"
    assert messages[-1]["content"] == FAILED_TURN_NOTICE
