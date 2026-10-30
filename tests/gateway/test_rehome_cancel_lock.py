"""A cancelled reset caller must not release the old owner's mutation_lock before the rehome commits."""
import asyncio
import threading

import pytest


@pytest.mark.asyncio
async def test_caller_cancelled_during_native_rehome_keeps_old_owner_lock(tmp_path, monkeypatch):
    import hermes_state_runtime
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_automation import reset_and_rehome_messages
    from gateway.session_ingress_context import native_callback
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    receipts = []
    for name in ('first', 'second'):
        event = MessageEvent(text=name, source=source, message_id=name)
        with native_callback(runner, event, get_hermes_home()):
            receipts.append(await authority.admit_native(event))
    key = runner.session_store._generate_session_key(source)
    old = runner.session_store.peek_session_id(key)
    live = authority.sessions[old]

    entered, release = threading.Event(), threading.Event()
    real = hermes_state_runtime.rehome_queued_inputs

    def suspended(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        return real(*args, **kwargs)
    monkeypatch.setattr(hermes_state_runtime, 'rehome_queued_inputs', suspended)
    try:
        caller = asyncio.ensure_future(reset_and_rehome_messages(runner, key, old))
        assert await asyncio.to_thread(entered.wait, 30)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # The move is still suspended: the old drain's claim fence must still be held.
        assert len(mutation_tasks(authority)) == 1
        assert live.mutation_lock.locked()
        assert len(list_session_admissions(authority.db, session_id=old, pending_only=True)) == 2
        release.set()
        await asyncio.gather(*mutation_tasks(authority))
        await asyncio.wait_for(live.mutation_lock.acquire(), 30)
        live.mutation_lock.release()
        fresh = runner.session_store.lookup_by_session_key(key).session_id
        assert fresh != old
        assert list_session_admissions(authority.db, session_id=old, pending_only=True) == []
        assert len(list_session_admissions(authority.db, session_id=fresh, pending_only=True)) == 2
    finally:
        release.set()
        runner.session_store.close_all_db_handles()
