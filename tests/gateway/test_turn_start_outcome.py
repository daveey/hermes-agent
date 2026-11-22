"""Canonical status is published before gateway transcript preparation."""
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


class Observed(BaseException):
    """Stop the real admission path before invoking a provider."""


@pytest.mark.parametrize("path", ["tui", "gateway"])
def test_turn_start_publication_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog, path):
    from contextlib import nullcontext
    from tui_gateway import server
    from tui_gateway.turn_outcome import refresh_turn_start_outcome
    from gateway.turn_outcome import refresh_gateway_turn_start_outcome

    def fail(*args, **kwargs):
        assert args == ("start", "turn_start")
        assert not kwargs.get("concluding_turn")
        raise OSError("unavailable fixture store")

    monkeypatch.setattr(so, "refresh_outcome", fail)
    if path == "tui":
        monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda session: nullcontext())
        monkeypatch.setattr(server, "_session_db", lambda session: nullcontext(object()))
        refresh_turn_start_outcome({"session_key": "start"})
    else:
        refresh_gateway_turn_start_outcome(SimpleNamespace(_session_db=object()), SimpleNamespace(session_id="start"))
    assert "unavailable fixture store" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "waiting"])
async def test_gateway_admission_publishes_running(tmp_path, monkeypatch, previous):
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    so._reset_hooks_for_tests()
    db = SessionDB(tmp_path / "state.db")
    db.create_session("start", source="discord")
    if previous:
        so.register_outcome_provider("queued", lambda sid: {"waiting_on": [
            {"kind": "card", "id": "card", "state": "todo", "label": "queued"}]})
        so.refresh_outcome("start", "manual", db=db)
        assert so.get_outcome("start", db=db)["status"]["kind"] == previous
    else:
        assert so.get_outcome("start", db=db) is None
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", user_id="user")
    event = MessageEvent(text="work", source=source)
    entry = SessionEntry(session_id="start", session_key="discord:start",
                         created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._session_db = db
    runner._hmwa_open_session = AsyncMock(return_value=(False, True))
    runner._set_session_env = lambda context: {}
    runner._pinned_session_context_prompt = lambda *a, **kw: ""

    async def acquire(*args):
        assert db.try_acquire_session_turn_lease("start", "admitted", ttl_seconds=300)

    async def inspect(*args):
        stored = so.get_outcome("start", db=db)
        assert stored is not None
        assert stored["status"]["kind"] == "running"
        assert stored["status"]["presentation"]["reason_codes"] == ["own_turn_active"]
        raise Observed

    runner._hmwa_acquire_turn_lease = acquire
    runner._mark_durable_active_turn = AsyncMock(return_value=True)
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store, load_transcript=inspect)
    try:
        with pytest.raises(Observed):
            await runner._hmwa_prepare_turn(event, source, entry, entry.session_key, entry.session_key, 1)
        db.release_session_turn_lease("start", "admitted")
        db.append_message("start", role="user", content="work")
        db.append_message("start", role="assistant", content="Finished.")
        so.refresh_outcome("start", "turn_end", db=db,
                           concluding_turn=so.turn_identity(lease_holder="admitted"))
        assert so.get_outcome("start", db=db)["status"]["kind"] == (previous or "done")
    finally:
        db.close()
        so._reset_hooks_for_tests()
