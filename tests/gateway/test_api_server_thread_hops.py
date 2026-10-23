"""Worker-thread hops onto the loop must be flushable before a terminal event / EOS is enqueued.

On CPython 3.14.7 a ``call_soon_threadsafe`` issued by the executor thread can run AFTER the loop
sees that thread's ``run_in_executor`` future complete, so ``assistant.completed``/EOS overtook the
last ``assistant.commentary`` event and the SSE writer never sent it (flaky
test_interim_commentary_reaches_session_sse_and_responses_stream on the hub runners).
"""

import asyncio

import pytest

from gateway.platforms.api_server import ThreadSafeAsyncQueue, _SessionEventQueue


def _hold_hops(loop):
    """Park worker-thread hops until the test releases them (deterministic slow hop)."""
    real, held = loop.call_soon_threadsafe, []
    loop.call_soon_threadsafe = lambda cb, *args, **kw: held.append((cb, args))

    def release():
        loop.call_soon_threadsafe = real
        for cb, args in held:
            loop.call_soon(cb, *args)

    return release


@pytest.mark.asyncio
async def test_session_event_queue_flush_waits_for_slow_worker_hop():
    loop = asyncio.get_running_loop()
    events = _SessionEventQueue("s", "r")
    release = _hold_hops(loop)
    await loop.run_in_executor(None, lambda: events.enqueue("assistant.commentary", {"text": "hi"}))
    assert events.queue.empty()  # the hop has not landed yet: the old code sent "completed" here
    flusher = asyncio.create_task(events.flush_hops())
    await asyncio.sleep(0.02)
    assert not flusher.done()  # flush is really waiting on the hop
    release()
    await flusher
    name, payload = events.queue.get_nowait()
    assert name == "assistant.commentary" and payload["text"] == "hi"
    assert events._hops_pending == 0


@pytest.mark.asyncio
async def test_threadsafe_queue_flush_waits_for_slow_worker_hop():
    loop = asyncio.get_running_loop()
    q = ThreadSafeAsyncQueue()
    release = _hold_hops(loop)
    await loop.run_in_executor(None, lambda: q.put_threadsafe("delta"))
    assert q.empty()
    flusher = asyncio.create_task(q.flush_hops())
    await asyncio.sleep(0.02)
    assert not flusher.done()
    release()
    await flusher
    assert q.get_nowait() == "delta"


@pytest.mark.asyncio
async def test_flush_hops_is_bounded_when_a_hop_never_runs():
    events = _SessionEventQueue("s", "r")
    events._hops_pending = 1
    await events.flush_hops(timeout=0.05)  # returns instead of hanging
