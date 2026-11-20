"""Drain custody for gateway housekeeping work crossing a sync/async boundary."""
from __future__ import annotations


def run_chore(logger, label, fn, *args, **kwargs) -> None:
    """Fence one pass, not the infrastructure loop; log failures without killing it."""
    from hermes_cli.worker_drain_fence import admit, lease_scope

    lease = None
    try:
        lease = admit('gateway.housekeeping')
        with lease_scope(lease):
            fn(*args, **kwargs)
    except Exception as exc:  # health: allow BLE001 -- chore failures are isolated and logged
        logger.debug('%s error: %s', label, exc)
    finally:
        if lease is not None:
            lease.close()


def schedule_chore(factory, loop, **kwargs):
    """Retain independent custody until the scheduled coroutine really completes.

    A concurrent Future's cancellation can precede asyncio cancellation cleanup.
    Revoke daemon exemption before releasing in that case; no unleased survivor
    can then be mistaken for idle. A caller's result(timeout=...) releases nothing.
    The factory is called only inside the admitted coroutine, never at enqueue.
    """
    from agent.async_utils import safe_schedule_threadsafe
    from hermes_cli.worker_drain_fence import admit, lease_scope

    lease = admit('gateway.housekeeping.async')

    async def run():
        with lease_scope(lease):
            return await factory()

    coro = run()
    try:
        future = safe_schedule_threadsafe(coro, loop, **kwargs)
    except BaseException:
        coro.close()
        if lease is not None:
            lease.close()
        raise
    if future is None:
        if lease is not None:
            lease.close()
        return None

    def finished(done):
        if done.cancelled():
            from hermes_cli.worker_drain_capability import revoke
            revoke('housekeeping async cancellation may still be cleaning up')
        if lease is not None:
            lease.close()

    future.add_done_callback(finished)
    return future
