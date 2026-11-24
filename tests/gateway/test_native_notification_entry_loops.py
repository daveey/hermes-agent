"""Both gateway entry loops consume native hints without a generic registry."""
import asyncio

import pytest

from gateway import kanban_watchers as watchers
from hermes_cli import config
from tui_gateway import notification_loaded_f as loaded
from tui_gateway import notification_gateway_sources as sources


@pytest.mark.parametrize('remote', [False, True])
def test_gateway_native_entry_loop_and_off_keep_single_executor(monkeypatch, remote):
    clock = [0.0]
    scheduler = loaded.Scheduler(clock=lambda: clock[0])
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setattr(sources, 'inventory', lambda: ((), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    monkeypatch.setattr(config, 'load_config', dict)
    monkeypatch.setattr(watchers.time, 'monotonic', lambda: clock[0])
    calls = []

    class Runner(watchers.GatewayKanbanWatchersMixin):
        _running = True
        _KANBAN_WATCHER_START_DELAY = 0

        def _active_profile_name(self):
            return 'default'

    runner = Runner()

    def collect(*args, report, **kwargs):
        calls.append(clock[0])
        if clock[0] == 1:
            report.retry()
            report.due_at(3)
        return []

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def wait(awaitable, *, timeout):
        awaitable.close()
        clock[0] += 1
        if clock[0] == 5 and scheduler.gateway_sources is not None:
            scheduler.gateway_sources.changed()
        if clock[0] == 6:
            monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
        if clock[0] == 11:
            runner._running = False
        scheduler.step()

    from gateway import kanban_remote_owner

    async def tick(*args, **kwargs):
        return collect(*args, **kwargs)

    async def legacy_sleep(interval):
        await wait(asyncio.Event().wait(), timeout=interval)

    monkeypatch.setattr(runner, '_sleep_between_ticks', legacy_sleep)
    monkeypatch.setattr(watchers, '_notifier_collect', collect)
    monkeypatch.setattr(kanban_remote_owner, 'tick', tick)
    monkeypatch.setattr(watchers.asyncio, 'to_thread', inline)
    monkeypatch.setattr(watchers.asyncio, 'wait_for', wait)
    monkeypatch.setattr(watchers, '_gc_retention_days', lambda: 30)
    try:
        entry = runner._kanban_remote_owner_watcher if remote else runner._kanban_notifier_watcher
        asyncio.run(entry(interval=5))
        assert calls == [1, 3, 5, 10]
        assert not scheduler.gateway_sources.actions
    finally:
        scheduler.close()


@pytest.fixture(autouse=True)
def native_policy_opt_in(monkeypatch):
    from hermes_cli import config
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    monkeypatch.setattr(config, 'load_config_readonly',
                        lambda: {'display': {'gateway_notification_f': True}})
