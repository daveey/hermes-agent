"""Cooperative cancellation at sync transaction boundaries, scoped to one loop thread."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

_running: ContextVar[Callable[[], bool] | None] = ContextVar("kanban_sync_running", default=None)


class SyncStopped(Exception):
    """Normal gateway shutdown/yield, not a failed sync round."""


@contextmanager
def stop_scope(keep_running: Callable[[], bool]):
    token = _running.set(keep_running)
    try:
        yield
    finally:
        _running.reset(token)


def run_loop(loop, keep_running: Callable[[], bool], interval) -> None:
    """Keep shutdown normal, crashes visible, and diagnostics cleanup unconditional."""
    from hermes_cli import kanban_sync_runstate as runstate, kanban_sync_timing as timing
    try:
        with stop_scope(keep_running):
            loop(keep_running, interval, runstate)
    except SyncStopped:
        runstate.record_down("stopped (gateway stopping, or the host sync lease went to another gateway)")
    except BaseException as exc:
        runstate.record_down(f"kanban sync loop crashed: {type(exc).__name__}: {exc}")
        raise
    finally:
        timing.stop()


def checkpoint() -> None:
    running = _running.get()
    if running is not None and not running():
        raise SyncStopped()
