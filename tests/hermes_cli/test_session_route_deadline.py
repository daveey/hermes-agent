"""Review probe: aggregate admission must retain the route queue deadline."""
import asyncio
import threading
import pytest
from hermes_cli import web_thread_lanes as lanes
from hermes_cli.web_routers import sessions
from tests.waiting import async_wait_until

@pytest.mark.asyncio
async def test_distinct_burst_keeps_queue_deadline(monkeypatch):
    lanes.configure(dict(lanes.DEFAULT_SIZES), queue_deadline_s=0.5)
    release = threading.Event()
    started = []
    def hold():
        started.append(True)
        assert release.wait(10)
    holders = [asyncio.create_task(lanes.run_in_lane('kanban', hold)) for _ in range(4)]
    await async_wait_until(lambda: len(started) == 4, timeout=5)
    monkeypatch.setattr(sessions, 'session_kanban_tasks', lambda *a, **k: [])
    requests = [asyncio.create_task(sessions.get_session_kanban_tasks(None, str(i))) for i in range(129)]
    try:
        # Observe beyond two full queue budgets, without releasing any lane slot.
        done, pending = await asyncio.wait(requests, timeout=1.25)
        print('deadline probe:', len(done), 'completed;', len(pending), 'still queued')
        assert not pending, 'coalescer adds unbounded waiting before lane deadline begins'
    finally:
        release.set()
        await asyncio.gather(*holders, *requests, return_exceptions=True)
        lanes.configure(dict(lanes.DEFAULT_SIZES), lanes.QUEUE_DEADLINE_S)
