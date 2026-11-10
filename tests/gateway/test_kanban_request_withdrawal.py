"""Withdrawal receipts traverse real gateway send/wake/ack dispatch."""
import asyncio
from contextlib import closing

import pytest

from gateway import kanban_watchers_notifier as notifier

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.gateway import test_kanban_changes_requested_notifier as helpers


@pytest.mark.parametrize("mode", ["notify+wake", "wake"])
def test_withdrawal_send_failure_keeps_subscription_until_retry(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "withdrawal.db"))
    kb.init_db()
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Decision", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           thread_id="topic-7", delivery_mode=mode)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', human_request=? WHERE id=?",
                         ('{"action":"Choose"}', tid))
        kb.archive_task(conn, tid)
        adapter = helpers.RecordingAdapter(fail_send=True)
        runner = helpers._runner(adapter)
        async def fail_wake(event):
            raise RuntimeError("wake unavailable")
        adapter.handle_message = fail_wake
        for _ in range(notifier.MAX_SEND_FAILURES + 1):
            runner._running = True
            asyncio.run(helpers._run_one_tick(monkeypatch, runner))
            assert kbn.list_notify_subs(conn, task_id=tid)
        assert adapter.handled == []
        adapter = helpers.RecordingAdapter()
        asyncio.run(helpers._run_one_tick(monkeypatch, helpers._runner(adapter)))
        assert len(adapter.sent) == (1 if mode == "notify+wake" else 0)
        if adapter.sent:
            assert "request withdrawn" in adapter.sent[0]["text"]
            assert "not proof of completion" in adapter.sent[0]["text"]
        assert len(adapter.handled) == 1
        text = adapter.handled[0].text
        assert "No answer is needed" in text
        assert "not approval" in text and "not proof of completion" in text
        assert "gateway.kanban.wake.request_withdrawn" not in text
        assert kbn.list_notify_subs(conn, task_id=tid) == []
        asyncio.run(helpers._run_one_tick(monkeypatch, helpers._runner(adapter)))
        assert len(adapter.handled) == 1
