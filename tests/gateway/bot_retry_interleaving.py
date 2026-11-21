"""Controlled receipt publication schedule for the Bot retry regression."""
import asyncio

import pytest_asyncio


@pytest_asyncio.fixture(params=['ordinary', 'refusal_before_projection'])
async def receipt_interleaving(bot, monkeypatch, request):
    if request.param == 'ordinary':
        yield
        return

    from gateway import session_bot, session_finite
    from tests.waiting import async_wait_until
    from tools import bot_live_delivery

    key = 'e' * 32
    run_error, published, observed, refresh_done = (asyncio.Event() for _ in range(4))
    loop = asyncio.get_running_loop()
    execute = session_finite.execute_finite_admission
    deliver = session_bot.deliver
    maybe_retry = session_bot._maybe_retry
    write = session_bot.write_receipt
    read = bot_live_delivery.read_delivery_result
    record_reply = session_bot._record_reply
    refreshes = []

    async def held_execution(authority, ref, row):
        if row['request_id'] == 'bot:' + key:
            await run_error.wait()
        return await execute(authority, ref, row)

    async def held_watcher(authority, home, delivery_id, future):
        if delivery_id == key:
            await refresh_done.wait()
        return await record_reply(authority, home, delivery_id, future)

    async def retry_after_settlement(authority, home, path, record):
        if record['delivery_id'] == key and record['status'] in {'queued', 'claimed'}:
            # refresh_receipt has read the pending projection. The physical settlement can
            # commit on its worker thread before _maybe_retry reads retry eligibility.
            run_error.set()
            await async_wait_until(
                lambda: all(row['status'] == 'terminal' for row in _rows(bot)),
                what='the original overflow admission to commit')
        return await maybe_retry(authority, home, path, record)

    async def held_write(home, record, **kwargs):
        result = await write(home, record, **kwargs)
        if (record['delivery_id'] == key and (record.get('retry') or {}).get('refused')
                and 'reason' not in record):
            published.set()
            await observed.wait()
        return result

    def observed_read(home, delivery_id):
        record = read(home, delivery_id)
        if delivery_id == key and (record or {}).get('retry', {}).get('refused'):
            loop.call_soon_threadsafe(observed.set)
        return record

    async def refresh(authority, home):
        try:
            await session_bot.refresh_receipt(authority, home, key)
        finally:
            refresh_done.set()

    async def scheduled_deliver(connection, params):
        result = await deliver(connection, params)
        if params['id'] == key:
            refreshes.append(asyncio.create_task(refresh(connection.authority, bot.home)))
            await asyncio.wait_for(published.wait(), 10)
        return result

    monkeypatch.setattr(session_finite, 'execute_finite_admission', held_execution)
    monkeypatch.setattr(session_bot, '_record_reply', held_watcher)
    monkeypatch.setattr(session_bot, '_maybe_retry', retry_after_settlement)
    monkeypatch.setattr(session_bot, 'write_receipt', held_write)
    monkeypatch.setattr(session_bot, 'deliver', scheduled_deliver)
    monkeypatch.setattr(bot_live_delivery, 'read_delivery_result', observed_read)
    try:
        yield
    finally:
        run_error.set()
        observed.set()
        refresh_done.set()
        for task in refreshes:
            await asyncio.wait_for(task, 10)
        from tests.gateway.test_session_bot_retry import _settle_quietly
        await _settle_quietly(bot)


def _rows(bot):
    from hermes_state_runtime import list_session_admissions
    return list_session_admissions(bot.authority.db, session_id=bot.chat, pending_only=False)
