"""Batch-level holds never rewind a partially accepted notifier batch."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from tests.gateway import test_kanban_wake_acceptance as helpers


@pytest.mark.asyncio
async def test_notifier_batch_hold_and_mid_batch_release(tmp_path, monkeypatch):
    from gateway import session_notifier_drain
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    (tmp_path / 'config.yaml').write_text('display: {suppress_warning_notifications: true}')
    runner, _adapter, _source, key = helpers.setup_route()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title='batch', assignee='worker', session_id=key)
        kbn.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='42',
                           user_id='42', chat_type='dm', delivery_mode='wake')
        kb.block_task(conn, tid, reason='failure', kind='transient')
        kb.complete_task(conn, tid, summary='result')
    finally:
        conn.close()
    held = [True]
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: held[0])
    delivered = []

    async def wake(self):
        delivered.append((self.synth, self.wake_diagnostic))
        held[0] = True  # hold begins after first accepted chunk

    monkeypatch.setattr(_KanbanNotification, 'wake', wake)

    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb,
            notifier_profile=None, gc_due=False, gc_retention_days=30)
        for delivery in deliveries:
            await _KanbanNotification(runner, delivery, platform_cls=Platform,
                                      sub_fail_counts={}).deliver()

    await tick()
    await tick()
    assert delivered == []
    held[0] = False
    await tick()
    assert [item[1] for item in delivered] == [True, False]
    held[0] = False
    await tick()
    assert len(delivered) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('notifier', [False, True])
async def test_native_wake_producer_is_explicit(monkeypatch, notifier):
    from gateway import wake, session_automation
    admit = AsyncMock(return_value=True)
    monkeypatch.setattr(session_automation, 'admit_route_wake', admit)
    adapter = SimpleNamespace(gateway_runner=object(), supports_async_delivery=True)
    await wake.deliver_wake(adapter, text='wake', source=object(), core_update_notifier=notifier)
    assert admit.await_args is not None
    assert admit.await_args.args[4] == ('kanban-wake' if notifier else 'background-wake')
