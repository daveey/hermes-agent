"""The onboarding fixture waits for admission settlement, not just lease release."""
import asyncio

import pytest

from tests.gateway.test_onboarding_on_the_authority import _desktop, _request, _settled


@pytest.mark.asyncio
async def test_settled_waits_for_the_opening_drain(tmp_path, monkeypatch):
    from gateway import session_settlement_recovery

    entered, release = asyncio.Event(), asyncio.Event()
    settle = session_settlement_recovery.settle_and_deliver

    async def held_settlement(*args, **kwargs):
        entered.set()
        await release.wait()
        return await settle(*args, **kwargs)

    monkeypatch.setattr(session_settlement_recovery, "settle_and_deliver", held_settlement)
    desktop = await _desktop(tmp_path, monkeypatch, [])
    waiter = None
    try:
        stored = (await _request(desktop, 1, "onboarding.start", {"rerun": True}))["result"]["stored_session_id"]
        await asyncio.wait_for(entered.wait(), timeout=30)
        with desktop.db._read_ctx() as conn:
            assert not desktop.db._guarded_ids(conn, [stored])
        drain = desktop.authority.sessions[stored].task
        assert not drain.done()
        waiter = asyncio.create_task(_settled(desktop, stored))
        # Give the waiter its first turn while settlement is explicitly held.
        await asyncio.sleep(0)
        assert not waiter.done(), "lease release is not admission settlement"
        release.set()
        await asyncio.wait_for(waiter, timeout=30)
        assert drain.done()
        row = desktop.db.get_session(stored)
        deleted = await _request(desktop, 2, "session.mutate", {
            "session_id": stored, "request_id": "delete-settled-onboarding",
            "expected_revision": row["runtime_revision"],
            "expected_generation": row["runtime_generation"],
            "operation": "delete", "payload": {},
        })
        assert "result" in deleted, deleted
    finally:
        release.set()
        if waiter is not None:
            await waiter
        for live in list(desktop.authority.sessions.values()):
            if live.task is not None:
                await asyncio.wait_for(asyncio.shield(live.task), timeout=30)
        await desktop.connection.close()
