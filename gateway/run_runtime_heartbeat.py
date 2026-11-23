"""Thread heartbeat plus a coalesced, owner-loop busy-count refresh [daveey]."""
import threading


def runtime_heartbeat(runner, loop):
    """Keep thread liveness independent of the loop; never sample authority state here.

    A wedged loop retains at most one callback. Once it runs, the next tick may
    enqueue another; closing a loop must not suppress the thread-side stamp.
    """
    pending = threading.Event()

    def refresh():
        try:
            runner._persist_active_agents()
        finally:
            pending.clear()

    def tick():
        from gateway.run import _write_runtime_status_quiet
        _write_runtime_status_quiet()
        if runner is None or loop is None or pending.is_set():
            return
        pending.set()
        try:
            loop.call_soon_threadsafe(refresh)
        except RuntimeError:  # loop closed during shutdown; retry only on a later tick
            pending.clear()
    return tick
