"""Messaging failure finalization dedup across durable claims and restarts."""
import asyncio

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from tests.gateway import test_kanban_notifier as helpers


@pytest.mark.parametrize("split", [True, False])
@pytest.mark.parametrize("delivery_mode", ["notify+wake", "wake"])
def test_final_failure_ping_and_wake_once(tmp_path, monkeypatch, split, delivery_mode):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    conn = kbc.connect()
    tid = kb.create_task(conn, title="failure fixture", assignee="worker")
    kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", delivery_mode=delivery_mode)
    expected_pings = int(delivery_mode == "notify+wake")
    adapter = helpers.RecordingAdapter()

    def emit(kind, run="run-one", **extra):
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, {"run_uid": run, **extra})

    def tick():
        # New runner each time: dedup must survive process-local state loss.
        asyncio.run(helpers._run_one_notifier_tick(monkeypatch, helpers._make_runner(adapter)))

    try:
        emit("reclaimed", failure_notice="canonical")
        if split:
            tick()
            assert not adapter.sent and not adapter.handled
        emit("crashed", failure_notice="canonical")
        emit("gave_up", failure_notice="canonical", failures=3, effective_limit=2)
        emit("failure_finalized", final_status="triage", failures=3, effective_limit=2)
        if not split:
            emit("failure_finalized", final_status="triage", failures=3, effective_limit=2)
        tick()
        assert len(adapter.sent) == expected_pings
        assert len(adapter.handled) == 1
        assert "automatic triage" in adapter.handled[0].text
        assert "gateway.kanban.wake.failure_finalized" not in adapter.handled[0].text
        assert "3 of 2" not in adapter.handled[0].text
        if expected_pings:
            assert "3 of 2" not in adapter.sent[0]["text"]
        emit("failure_finalized", final_status="triage", failures=3, effective_limit=2)
        tick()
        assert len(adapter.sent) == expected_pings
        assert len(adapter.handled) == 1
        emit("failure_finalized", run="run-two", final_status="ready", failures=1, effective_limit=2)
        tick()
        assert len(adapter.sent) == 2 * expected_pings
        assert len(adapter.handled) == 2
    finally:
        conn.close()


@pytest.mark.parametrize("failed_lane", ["ping", "wake"])
def test_failed_delivery_retries_only_missing_obligation(tmp_path, monkeypatch, failed_lane):
    from gateway.wake import WakeNotAccepted

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "retry.db"))
    kb.init_db()
    conn = kbc.connect()
    tid = kb.create_task(conn, title="retry fixture", assignee="worker")
    sub = dict(task_id=tid, platform="telegram", chat_id="chat-1")
    kbn.add_notify_sub(conn, **sub, delivery_mode="notify+wake")

    class FlakyAdapter(helpers.RecordingAdapter):
        failed = False

        async def send(self, *args, **kwargs):
            if failed_lane == "ping" and not self.failed:
                self.failed = True
                raise OSError("controlled send refusal")
            await super().send(*args, **kwargs)

        async def handle_message(self, event):
            if failed_lane == "wake" and not self.failed:
                self.failed = True
                raise WakeNotAccepted()
            await super().handle_message(event)

    adapter = FlakyAdapter()

    def tick():
        asyncio.run(helpers._run_one_notifier_tick(monkeypatch, helpers._make_runner(adapter)))

    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "failure_finalized", {
                "run_uid": "retry-run", "final_status": "triage", "failures": 1, "effective_limit": 1,
            })
        tick()
        assert adapter.failed
        assert len(adapter.sent) == int(failed_lane == "wake")
        assert not adapter.handled
        assert kbn.notify_failure_receipts(conn, **sub, lane="wake") == set()
        tick()
        assert len(adapter.sent) == 1
        assert len(adapter.handled) == 1
        for lane in ("ping", "wake"):
            assert kbn.notify_failure_receipts(conn, **sub, lane=lane) == {"retry-run"}
        tick()
        assert len(adapter.sent) == len(adapter.handled) == 1
    finally:
        conn.close()
