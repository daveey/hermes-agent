"""Run-owned outcome evidence survives timer delays and displaced gateway slots."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hermes_state_outcome as so
from gateway import turn_outcome as gw
from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tests.hermes_state import test_outcome_late_turn_end_refresh as late
from tests.hermes_state import test_outcome_turn_end_declaration as helpers
from tui_gateway import server, turn_outcome as tui
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

db = helpers.db
timers = late.timers


@pytest.mark.parametrize("explicit", [False, True])
def test_retry_targets_frozen_sid_after_agent_replacement(db, monkeypatch, timers, explicit):
    late._tui_scope(monkeypatch, db)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    session = {"session_key": "root", "agent": SimpleNamespace(session_id="old-tip"),
               "_refused_turn_delay": 45}
    calls = []
    monkeypatch.setattr(tui, "refresh_turn_outcome", lambda *a, **kw: calls.append(kw))
    concluding = frozenset({"lease:old"})
    kwargs = {"outcome_sid": "captured-tip"} if explicit else {}
    server._schedule_refused_turn_retry("ui", session, "missing-marker", concluding, **kwargs)
    session.update(agent=SimpleNamespace(session_id="new-tip"), session_key="new-root")
    timers[0]()
    assert calls == [{"concluding_turn": concluding,
                      "session_id": "captured-tip" if explicit else "old-tip"}]


def test_marker_read_failure_warns_and_preserves_unknown_marker(db, monkeypatch, caplog):
    late._tui_scope(monkeypatch, db)
    home = db.db_path.parent
    session = {"session_key": "chat", "profile_home": str(home)}
    assert record_turn_start(home, "chat", "still owned")
    marker = read_turn_marker(home, "chat")
    with monkeypatch.context() as patcher:
        def fail(*a):
            raise OSError("readback unavailable")
        patcher.setattr("tui_gateway.turn_marker.read_turn_marker", fail)
        with caplog.at_level(logging.WARNING):
            own = tui.own_turn_marker(session, "chat")
    assert own is None
    assert any(r.levelno == logging.WARNING and "own turn marker read failed" in r.message
               and r.exc_info for r in caplog.records)
    server._retire_turn_marker(session, "chat", concluding_turn=tui.concluding_turn_identity(marker=own))
    assert read_turn_marker(home, "chat") == marker


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("newer", [False, True])
async def test_displaced_handler_uses_worker_receipt_not_successor_slot(db, monkeypatch, failed, newer):
    """Real handler + conversation wrapper, copied executor context, and durable outcome refresh."""
    helpers._declare_then_reply(db)
    if newer:
        late._next_turn_starts(db)
    assert db.try_acquire_session_turn_lease("chat", "turn-B" if newer else "turn-A", ttl_seconds=300)
    entry = SimpleNamespace(session_id="chat")
    source = SimpleNamespace(platform=Platform.TELEGRAM, user_name="Example", user_id="u", chat_id="room")
    event = SimpleNamespace(text="A", reply_to_message_id=None, reply_to_text=None,
                            internal=True, channel_prompt=None, message_id="m", reply_expected=True,
                            message_type=None, metadata={}, _gateway_active_turn_token="token-A")
    prepared = SimpleNamespace(history=[], message_text="A", context_prompt="", title_user_message=None,
        persist_user_message=None, persist_user_timestamp=None, persist_user_display_kind=None,
        persistence_owner=None)
    successor = SimpleNamespace(_last_session_turn_lease_holder="turn-B", session_id="new-chat")
    state = SimpleNamespace(turn=SimpleNamespace(agent=successor))
    runner = SimpleNamespace(_session_db=db, _PreparedTurn=SimpleNamespace,
        _hmwa_resolve_session=AsyncMock(return_value=(source, entry, "key")),
        _hmwa_prepare_turn=AsyncMock(return_value=(prepared, [])),
        hooks=SimpleNamespace(emit=AsyncMock()), _pinned_channel_inputs=lambda *a, **kw: (None, source),
        _reply_anchor_for_event=lambda e: "m", _hmwa_stop_typing_for_turn=AsyncMock(),
        _is_session_run_current=lambda *a: False, _hmwa_discard_stale_result=lambda *a: None,
        _hmwa_agent_error_reply=AsyncMock(return_value=None), _clear_session_env=lambda *a: None,
        _session_state=lambda key: state)
    monkeypatch.setattr("gateway.run_heartbeat_acceptance.heartbeat_owner_is_current", lambda *a: True)
    agent = SimpleNamespace(session_id="chat", _last_session_turn_lease_holder="turn-A")
    def conversation(*a, **kw):
        if failed:
            raise ValueError("old run failed")
        return {"final_response": "done"}
    agent.run_conversation = conversation
    worker = object.__new__(TurnRunner)
    worker._ctx = TurnContext(message="A", session_id="chat", session_key="key", source=source)
    monkeypatch.setattr(worker, "_native_image_run_message", lambda: "A")
    receipts = []
    async def run_agent(**kw):
        try:
            from gateway.run import GatewayRunner
            executor_runner = SimpleNamespace(_get_executor=lambda: None)
            return await GatewayRunner._run_in_executor_with_context(
                executor_runner, worker._run_conversation_with_approval, agent, [], None, None, None)
        finally:
            receipts.append(dict(gw.run_outcome.get()))
            # Both the cached object and shared entry now belong to another turn.
            agent._last_session_turn_lease_holder = "turn-B"
            agent.session_id = entry.session_id = "new-chat"
    runner._run_agent = run_agent
    threads = []
    real_refresh = gw.refresh_gateway_turn_outcome
    def refresh(*a):
        assert receipts, runner._hmwa_agent_error_reply.call_args
        assert gw.concluding_turn_identity(event) == {"lease:turn-A", "gateway:token-A"}
        threads.append(real_refresh(*a))
    monkeypatch.setattr(gw, "refresh_gateway_turn_outcome", refresh)
    previous = gw.run_outcome.get()
    await GatewayTurnMixin._handle_message_with_agent(runner, event, source, "key", 1)
    assert gw.run_outcome.get() is previous
    assert receipts == [{"session_id": "chat", "lease_holder": "turn-A"}]
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert so.get_outcome("chat", db=db)["kind"] == ("running" if newer else "waiting")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("newer", [False, True])
@pytest.mark.parametrize("followup", [False, True])
async def test_abandoned_worker_captures_own_live_lease(db, monkeypatch, cancelled, newer, followup):
    helpers._declare_then_reply(db)
    if newer:
        late._next_turn_starts(db)
    assert db.try_acquire_session_turn_lease("chat", "turn-B" if newer else "turn-A", ttl_seconds=300)
    agent = SimpleNamespace(session_id="chat", _last_session_turn_lease_holder="turn-A",
                            _active_session_turn_lease_holder="turn-A")
    ctx = SimpleNamespace(agent_holder=[agent])
    pending = asyncio.get_running_loop().create_future()
    worker = SimpleNamespace(executor_task=pending, agent_timeout=1,
                             timeout_fired=SimpleNamespace(is_set=lambda: True))
    async def poll(*a, **kw):
        if cancelled:
            raise asyncio.CancelledError
        return set(), {pending}
    monkeypatch.setattr("gateway.run_turn_wait.asyncio.wait", poll)
    runner = SimpleNamespace(_session_db=db, _run_agent_timeout_result=lambda *a: {"failed": True},
                             _session_state=lambda *a: pytest.fail("successor slot read"))
    token = gw.run_outcome.set({"session_id": "chat"})
    tokens = []
    try:
        if followup:
            # A completed first conversation in the same handler left its own receipt.
            tokens.append(gw.begin_run_receipt("chat"))
            gw.capture_run_outcome(SimpleNamespace(session_id="chat", _last_session_turn_lease_holder="turn-A1"))
        tokens.append(gw.begin_run_receipt("chat"))  # this (abandoned) run's receipt
        call = GatewayTurnMixin._run_agent_await_turn_worker(runner, worker, ctx, None, None)
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            assert await call == {"failed": True}
        assert not pending.done()  # worker still owns its physical lease
        event = SimpleNamespace(_gateway_active_turn_token="token-A")
        assert gw.concluding_turn_identity(event) == {"lease:turn-A", "gateway:token-A"}
        thread = gw.refresh_gateway_turn_outcome(runner, SimpleNamespace(session_id="new-chat"), event, "key")
        thread.join(10)
        assert not thread.is_alive()
        assert so.get_outcome("chat", db=db)["kind"] == ("running" if newer else "waiting")
    finally:
        pending.cancel()
        for t in reversed(tokens):
            gw.run_receipt.reset(t)
        gw.run_outcome.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", [None, "turn-A"])
@pytest.mark.parametrize("failed", [False, True])
async def test_finished_worker_preserves_frozen_receipt(holder, failed):
    owned = {"session_id": "chat", "lease_holder": holder}
    token = gw.run_outcome.set(owned.copy())
    completed = asyncio.get_running_loop().create_future()
    if failed:
        completed.set_exception(ValueError("worker failed"))
    else:
        completed.set_result({"done": True})
    worker = SimpleNamespace(executor_task=completed)
    ctx = SimpleNamespace(agent_holder=[SimpleNamespace(
        session_id="new-chat", _last_session_turn_lease_holder="turn-B")])
    try:
        call = GatewayTurnMixin._run_agent_await_turn_worker(None, worker, ctx, None, None)
        if failed:
            with pytest.raises(ValueError, match="worker failed"):
                await call
        else:
            assert await call == {"done": True}
        assert gw.run_outcome.get() == owned
    finally:
        gw.run_outcome.reset(token)


def test_null_sid_does_not_refresh(monkeypatch):
    monkeypatch.setattr(gw, "_refresh", lambda *a: pytest.fail("null session refreshed"))
    token = gw.run_outcome.set({"session_id": None})
    try:
        runner = SimpleNamespace(_session_db=object())
        assert gw.refresh_gateway_turn_outcome(runner, None) is None
        gw.capture_run_outcome(SimpleNamespace(session_id=None))
        assert gw.run_outcome.get()["session_id"] == ""
    finally:
        gw.run_outcome.reset(token)


def test_no_run_scope_never_names_successor():
    token = gw.run_outcome.set(None)
    try:
        gw.capture_run_outcome(SimpleNamespace(_last_session_turn_lease_holder="foreign"))
        assert gw.concluding_turn_identity() == frozenset()
    finally:
        gw.run_outcome.reset(token)
