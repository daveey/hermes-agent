"""Per-``_run_agent`` outcome receipts through a real recursive queued follow-up.

Drives the real ``GatewayRunner._run_agent`` → ``_run_agent_queued_followup`` → ``_run_agent`` recursion with a fake
agent (no stubbed ``_run_agent``): each invocation owns its receipt, so a predecessor worker writing late can never
rename the handler's concluding turn, and an abandoned worker never drains the follow-up.
"""
import asyncio
import contextvars
import threading
from types import SimpleNamespace

import pytest

from gateway import turn_outcome as gw
from gateway.platforms.base import MessageEvent
from tests.gateway.test_queued_followup_processing_hooks import (
    SESSION_KEY,
    HookRecordingAdapter,
    _install_fake_agent,
    _make_runner,
    _source,
)

EVENT = SimpleNamespace(_gateway_active_turn_token="token-A")


@pytest.fixture
def handler_scope():
    """What ``_handle_message_with_agent`` opens around its ``_run_agent`` call."""
    token = gw.run_outcome.set({"session_id": "chat"})
    try:
        yield
    finally:
        gw.run_outcome.reset(token)


@pytest.mark.asyncio
async def test_followup_owns_the_concluding_receipt_despite_a_late_predecessor_write(
        monkeypatch, tmp_path, handler_scope):
    contexts, calls = [], []

    class LeasedTurns:
        def __init__(self, **kwargs):
            self.tools, self.session_id = [], kwargs.get("session_id")

        def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
            calls.append(message)
            # The turn's lease; the worker context is the one its own late capture would write through.
            self._last_session_turn_lease_holder = f"turn-{len(calls)}"
            contexts.append(contextvars.copy_context())
            return {"final_response": f"done-{len(calls)}", "messages": [], "api_calls": 1}

    _install_fake_agent(monkeypatch, tmp_path, LeasedTurns)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(text="follow-up", source=_source(), message_id="queued")
    result = await runner._run_agent(message="first", context_prompt="", history=[], source=_source(),
                                     session_id="chat", session_key=SESSION_KEY)
    assert calls == ["first", "follow-up"] and result["final_response"] == "done-2"
    assert gw.concluding_turn_identity(event=EVENT) == {"lease:turn-2", "gateway:token-A"}
    # The predecessor's worker captures again after its successor ran (an abandoned worker's late write).
    stale = SimpleNamespace(session_id="old-chat", _last_session_turn_lease_holder="turn-1")
    contexts[0].run(gw.capture_run_outcome, stale)
    assert gw.concluding_turn_identity(event=EVENT) == {"lease:turn-2", "gateway:token-A"}
    assert gw.concluding_receipt()["session_id"] == "chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("leased", [False, True])
async def test_abandoned_worker_never_drains_and_names_only_its_own_lease(
        monkeypatch, tmp_path, handler_scope, leased):
    started, release, calls = threading.Event(), threading.Event(), []

    class WedgedFirstTurn:
        def __init__(self, **kwargs):
            self.tools, self.session_id = [], kwargs.get("session_id")
            # A cached agent remembers its previous turn's (released) lease.
            self._last_session_turn_lease_holder = "previous-turn"
            self._active_session_turn_lease_holder = None

        def get_activity_summary(self):
            return {"seconds_since_activity": 3600.0}

        def interrupt(self, *a, **kw):
            pass

        def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
            calls.append(message)
            if leased:
                self._active_session_turn_lease_holder = self._last_session_turn_lease_holder = "turn-1"
            started.set()
            assert release.wait(10), "abandoned worker was never released"
            return {"final_response": "late", "messages": [], "api_calls": 1}

    _install_fake_agent(monkeypatch, tmp_path, WedgedFirstTurn)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0.01")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT_WARNING", "0")
    real_wait = asyncio.wait

    async def quick_wait(fs, timeout=None, **kw):
        return await real_wait(fs, timeout=min(timeout or 0.05, 0.05), **kw)

    monkeypatch.setattr("gateway.run_turn_wait.asyncio.wait", quick_wait)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    queued = MessageEvent(text="follow-up", source=_source(), message_id="queued")
    adapter._pending_messages[SESSION_KEY] = queued
    try:
        result = await asyncio.wait_for(runner._run_agent(
            message="first", context_prompt="", history=[], source=_source(),
            session_id="chat", session_key=SESSION_KEY), 30)
        assert started.is_set() and result.get("failed")
        # Drain gating: abandonment returns the synthetic failure and leaves the follow-up queued.
        assert calls == ["first"]
        assert adapter._pending_messages.get(SESSION_KEY) is queued
        want = {"lease:turn-1", "gateway:token-A"} if leased else {"gateway:token-A"}
        assert gw.concluding_turn_identity(event=EVENT) == want
    finally:
        release.set()
