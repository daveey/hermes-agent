"""The embedded dispatcher's between-tick sleep ends early when an applied
remote sync change made work claimable (core patch 0193), and otherwise keeps
``_sleep_between_ticks`` semantics."""

from __future__ import annotations

import asyncio
import time

import pytest

from gateway import kanban_watchers as watchers
from hermes_cli import kanban_db_sync as sync


@pytest.fixture
def runner():
    sync.consume_dispatcher_wake()
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    return runner


def test_sync_wake_cuts_the_tick_sleep_short(runner):
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, sync.wake_dispatcher)
        started = time.monotonic()
        await runner._sleep_until_tick_or_sync_wake(60)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5
    assert sync.consume_dispatcher_wake() is False  # the wake was consumed by the sleep


def test_without_a_wake_the_sleep_runs_to_its_end(runner, monkeypatch):
    calls = []

    async def between(interval):
        calls.append(interval)

    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    asyncio.run(runner._sleep_until_tick_or_sync_wake(7))
    assert calls == [7]


def test_cancellation_inside_the_sleep_still_propagates(runner, monkeypatch):
    async def between(interval):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner._sleep_until_tick_or_sync_wake(7))
