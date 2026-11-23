"""The gateway notifier retains one executor across facts, retries and rollback."""
import asyncio

import pytest

from gateway import kanban_watchers as watchers
from hermes_cli import config
from hermes_cli.fact_contract import Fact, FactKey
from hermes_cli.fact_fake import FakeStore
from tests.tui_gateway import test_notification_facts as helpers
from tui_gateway import notification_facts


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("retry", [False, True])
def test_real_notifier_loop_quiet_retry_delta_and_rollback(monkeypatch, retry, remote):
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = notification_facts.bind(store, helpers.SCOPE, "/canonical/notifier")
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    clock = [0.0]
    calls = []

    class Runner(watchers.GatewayKanbanWatchersMixin):
        _running = True
        _KANBAN_WATCHER_START_DELAY = 0

        def _active_profile_name(self):
            return "default"

    runner = Runner()
    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(watchers.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watchers, "_gc_retention_days", lambda: 30)

    def collect(*args, report, **kwargs):
        calls.append(clock[0])
        if retry and len(calls) == 1:
            report.retry()
        return []

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def wait(awaitable, *, timeout):
        awaitable.close()
        clock[0] += 1
        if clock[0] == 6:
            store.publish([Fact(FactKey(helpers.SCOPE, "SourceState", "board"), 1)])
            store.drain()
        if clock[0] == 7:
            store.rollback(helpers.SCOPE)
        if clock[0] == 12:
            runner._running = False

    monkeypatch.setattr(watchers, "_notifier_collect", collect)
    monkeypatch.setattr(watchers.asyncio, "to_thread", inline)
    monkeypatch.setattr(watchers.asyncio, "wait_for", wait)
    from gateway import kanban_remote_owner

    async def tick(*args, **kwargs):
        return collect(*args, **kwargs)

    monkeypatch.setattr(kanban_remote_owner, "tick", tick)
    try:
        watcher = runner._kanban_remote_owner_watcher if remote else runner._kanban_notifier_watcher
        asyncio.run(watcher(interval=5))
        assert calls == ([0, 5, 6, 11] if retry else [0, 6, 11])
        assert not owner.actions
    finally:
        owner.close()
