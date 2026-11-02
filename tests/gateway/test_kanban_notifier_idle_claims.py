"""Idle filtering must preserve recovery of expired claims held by live processes."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import kanban_watchers_notifier as watcher
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify


@pytest.mark.parametrize('expired', [False, True])
def test_collector_retains_live_claims_for_expiry_recovery(tmp_path, monkeypatch, expired):
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    conn = kbc.connect(path)
    try:
        task = kb.create_task(conn, title='held notification', assignee='default')
        notify.add_notify_sub(conn, task_id=task, platform='telegram', chat_id='chat')
        before = notify.list_notify_subs(conn)[0]['last_event_id']
        with kbc.write_txn(conn):
            kb._append_event(conn, task, 'completed', {})
            last = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id = ?', (task,)).fetchone()[0]
            age = notify.NOTIFY_CLAIM_STALE_S + 60 if expired else 0
            conn.execute('UPDATE kanban_notify_subs SET inflight_from=?,last_event_id=?,inflight_pid=12345,'
                         'inflight_start=NULL,inflight_at=?', (before, last, int(time.time()) - age))
        monkeypatch.setattr(notify, '_holder_alive', lambda row: True)
        monkeypatch.setattr(notify, '_holder_is_me', lambda row: False)
        runner = SimpleNamespace(_owns_kanban_dispatcher_lock=lambda: True,
                                 adapters={'telegram': object()}, _profile_adapters={},
                                 config=SimpleNamespace(multiplex_profiles=False))
        monkeypatch.setattr(watcher, '_adapter_for_subscription', lambda *a: object())
        collector = watcher._Collector(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
        with kb.pin_first_board_resolution():
            collector.collect_board('default')
        assert len(collector.deliveries) == int(expired)
        if expired:
            assert [event.kind for event in collector.deliveries[0]['events']] == ['completed']
    finally:
        conn.close()


def test_collector_does_not_claim_idle_subscription(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    conn = kbc.connect(path)
    try:
        task = kb.create_task(conn, title='idle notification', assignee='default')
        notify.add_notify_sub(conn, task_id=task, platform='telegram', chat_id='chat')
        assert len(notify.list_notify_subs(conn)) == 1
        claim = Mock(wraps=notify.claim_unseen_events_for_sub)
        monkeypatch.setattr(notify, 'claim_unseen_events_for_sub', claim)
        runner = SimpleNamespace(_owns_kanban_dispatcher_lock=lambda: True,
                                 adapters={'telegram': object()}, _profile_adapters={},
                                 config=SimpleNamespace(multiplex_profiles=False))
        monkeypatch.setattr(watcher, '_adapter_for_subscription', lambda *a: object())
        collector = watcher._Collector(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
        with kb.pin_first_board_resolution():
            collector.collect_board('default')
        claim.assert_not_called()
        assert collector.deliveries == []
    finally:
        conn.close()
