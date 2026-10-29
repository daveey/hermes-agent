"""A messaging-platform turn refreshes the chat's stored outcome when it concludes, however it
concluded (``GatewayRunner._handle_message_with_agent``'s finally path), off the reply path."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hermes_state_outcome as so
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_state import SessionDB

SUMMARY = "Booked the table for four at 7pm and added it to the shared calendar with the address."


def _runner(db, entry, source, run_agent):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._session_db = SimpleNamespace(_db=db)
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, entry.session_key))
    prepared = GatewayRunner._PreparedTurn(
        history=[], context_prompt="", message_text="book dinner", persist_user_message=None,
        persist_user_timestamp=None, persist_user_display_kind=None)
    runner._hmwa_prepare_turn = AsyncMock(return_value=(prepared, {}))
    runner._clear_session_env = lambda tokens: None
    runner._pinned_channel_inputs = lambda key, prompt, source, internal=False: (prompt, source)
    runner._persist_prompt_pins = AsyncMock()
    runner._run_agent = run_agent
    runner._hmwa_agent_error_reply = AsyncMock(return_value=None)
    return runner


def _wait_for_outcome(db, sid: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = so.get_outcome(sid, db=db)
        if stored is not None:
            return stored
        time.sleep(0.02)
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, kind", [(SUMMARY, "done"), (None, "unresolved")])
async def test_gateway_turn_end_refreshes_the_stored_outcome(tmp_path, reply, kind):
    so._reset_hooks_for_tests()
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("tg-chat", source="telegram")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", user_name="Dana")
    entry = SessionEntry(session_id="tg-chat", session_key="telegram:c1",
                         created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))

    async def run_agent(**_kwargs):
        db.append_message("tg-chat", role="user", content="book dinner")
        if reply is None:
            raise RuntimeError("provider exploded")
        db.append_message("tg-chat", role="assistant", content=reply)
        raise RuntimeError("delivery stops here; the transcript is already persisted")

    runner = _runner(db, entry, source, run_agent)
    event = MessageEvent(text="book dinner", source=source)
    try:
        await runner._handle_message_with_agent(event, source, entry.session_key, 1)
        stored = _wait_for_outcome(db, "tg-chat")
        assert stored is not None and stored["kind"] == kind
        if kind == "unresolved":
            assert stored["reason"] == "interrupted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_reply_returns_while_a_slow_refresh_is_still_running(tmp_path, monkeypatch):
    so._reset_hooks_for_tests()
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("tg-slow", source="telegram")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c2", user_id="u1", user_name="Dana")
    entry = SessionEntry(session_id="tg-slow", session_key="telegram:c2",
                         created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_refresh(sid, trigger, *, db=None):
        started.set()
        release.wait(10)
        finished.set()

    monkeypatch.setattr(so, "refresh_outcome", slow_refresh)

    async def run_agent(**_kwargs):
        raise RuntimeError("provider exploded")

    runner = _runner(db, entry, source, run_agent)
    try:
        await runner._handle_message_with_agent(MessageEvent(text="hi", source=source), source,
                                                entry.session_key, 1)
        assert started.wait(5), "the turn end must start the refresh"
        assert not finished.is_set(), "the handler returned only after the refresh finished"
    finally:
        release.set()
        assert finished.wait(5)
        db.close()
