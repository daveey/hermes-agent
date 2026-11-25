"""A cancelled gateway watcher must not leave bootstrap in asyncio's executor."""
import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_client as client


@pytest.mark.parametrize("snapshot", [False, True])
def test_gateway_stop_interrupts_active_replica_pages(monkeypatch, snapshot):
    entered = threading.Event()
    stopped = threading.Event()
    pages = []
    applied = []

    class Hub:
        def changes(self, board, cursor):
            pages.append(cursor)
            entered.set()
            assert stopped.wait(5)
            return {"changes": [], "next": cursor + 1, "more": len(pages) < 100}

        def snapshot_page(self, board, table, after):
            pages.append(after)
            entered.set()
            assert stopped.wait(5)
            return {"rows": [], "head": 99, "next": str(len(pages)), "more": len(pages) < 100}

    monkeypatch.setattr(client.sync, "sync_state", lambda conn: {"cursor": 0})
    monkeypatch.setattr(client.sync, "apply_remote_changes", lambda *a, **k: applied.append(k["new_cursor"]))
    monkeypatch.setattr(client, "pending_task_fields", lambda conn: {})
    monkeypatch.setattr(client, "IMPORT_ORDER", ["tasks"])

    def round_loop(keep_running, interval, runstate):
        # Native pull checks durable replay before fetching. Keep that real read,
        # with SQLite owned and closed by the executor thread using it.
        conn = sqlite3.connect(":memory:")
        try:
            if snapshot:
                client.resync(conn, Hub(), "default", observers=False)
            else:
                client.pull(conn, Hub(), "default", observers=False)
        finally:
            conn.close()

    monkeypatch.setattr(client, "_run_forever", round_loop)

    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from tests.waiting import async_wait_until
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    lease = SimpleNamespace(try_acquire=lambda: True, should_yield=lambda: False, release=lambda: None)
    monkeypatch.setattr(runner, "_kanban_singleton_lease", lambda *a: lease)
    monkeypatch.setattr(client.sync, "sync_enabled", lambda: True)

    async def gateway():
        task = asyncio.create_task(runner._kanban_sync_watcher())
        try:
            await async_wait_until(entered.is_set, timeout=5)
        finally:
            runner._running = False
            stopped.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    start = time.monotonic()
    asyncio.run(gateway())  # includes the default executor's join, not just task cancellation
    assert time.monotonic() - start < 5  # well inside hermes update's 30s wait
    assert len(pages) == 1, "shutdown must stop bootstrap before requesting another page"
    assert applied == [], "do not start applying a fetched page after shutdown"
