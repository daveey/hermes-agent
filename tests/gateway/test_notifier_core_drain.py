"""Core drain must leave receipted notifier wakes queued, without holding humans."""
import asyncio
import json
import os
import time

import pytest

from tests.gateway.test_wake_admitted_before_drain_authority import (
    Hold, admissions, authority_route,
)
from tests.waiting import async_wait_until

pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")  # symbolic profile ids are relative homes


@pytest.mark.asyncio
@pytest.mark.parametrize('release', ['apply', 'give-up'])
async def test_receipted_wakes_wait_for_core_drain_but_human_bypasses(monkeypatch, tmp_path, release):
    from agent import core_update_drain
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.wake import deliver_wake

    marker = tmp_path / "drain-hold"
    monkeypatch.setattr(core_update_drain, "hold_paths", lambda: [marker])
    executed, hold = [], Hold()
    runner, adapter, authority, source = await authority_route(monkeypatch, "owner", executed, hold)
    try:
        await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
        await asyncio.wait_for(hold.running.wait(), 10)
        for text in ("wake-1", "wake-2"):
            await deliver_wake(adapter, text=text, source=source, core_update_notifier=True)
        marker.write_text(json.dumps({"pid": os.getpid(), "reason": "fixture core drain"}))
        assert core_update_drain.active_hold() is not None
        hold.finish.set()
        await adapter.handle_message(MessageEvent(text="human", source=source, message_id="human"))
        await async_wait_until(
            lambda: admissions(runner, authority, source)[-1] == ("messaging", "terminal", "completed"),
            what="human bypasses deferred notifier wakes",
        )
        assert executed == [("owner", "A"), ("owner", "human")], (
            "core drain starved: receipted notifier wakes started new turns during hold", executed)
        assert admissions(runner, authority, source)[1:3] == [
            ("automation", "queued", None), ("automation", "queued", None)]
        await async_wait_until(lambda: runner._active_work_count() == 0,
                               what="settlement unwinds to an idle core-update census")
        if release == 'apply':
            marker.unlink()
        else:
            marker.write_text(json.dumps({'pid': os.getpid(), 'started_at': time.time() - 10, 'max_s': 1}))
        await async_wait_until(
            lambda: all(status == "terminal" for _, status, _ in admissions(runner, authority, source)),
            what="deferred wakes released after core drain",
        )
        # Both released wakes are pending at one boundary, so they ride one turn in admission order
        # (gateway/session_fold_pending.py, t_01a1d663082aa53a).
        assert executed == [("owner", text) for text in ("A", "human", "wake-1\n\nwake-2")]
    finally:
        hold.finish.set()
        marker.unlink(missing_ok=True)
        await settle_gateway_runtime(runner)


@pytest.mark.asyncio
async def test_new_human_wakes_held_pump_without_waiting_for_poll(monkeypatch, tmp_path):
    from agent import core_update_drain
    from gateway import session_authority
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.wake import deliver_wake

    marker = tmp_path / 'drain-hold'
    monkeypatch.setattr(core_update_drain, 'hold_paths', lambda: [marker])
    monkeypatch.setattr(session_authority, '_CLAIM_RETRY_MIN_S', 60)
    executed, hold = [], Hold()
    runner, adapter, authority, source = await authority_route(monkeypatch, 'owner', executed, hold)
    try:
        await adapter.handle_message(MessageEvent(text='A', source=source, message_id='A'))
        await asyncio.wait_for(hold.running.wait(), 10)
        await deliver_wake(adapter, text='wake', source=source, core_update_notifier=True)
        marker.write_text(json.dumps({'pid': os.getpid()}))
        hold.finish.set()
        await async_wait_until(lambda: runner._active_work_count() == 0, what='held pump idle')
        # The 60-second hold poll cannot be what admits this human.
        await adapter.handle_message(MessageEvent(text='human', source=source, message_id='human'))
        await async_wait_until(lambda: ('owner', 'human') in executed, timeout=5,
                               what='human admission signals the held pump')
        assert executed == [('owner', 'A'), ('owner', 'human')]
    finally:
        hold.finish.set()
        marker.unlink(missing_ok=True)
        for live in authority.sessions.values():
            live.claim_changed.set()
        await settle_gateway_runtime(runner)
