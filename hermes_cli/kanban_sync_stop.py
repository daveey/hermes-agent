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
    from hermes_cli.kanban_sync_claims import loop_started
    loop_started()  # this process's claims wait for its first round at the feed head (t_01a1d6c4e700958e)
    try:
        with stop_scope(keep_running):
            loop(keep_running, interval, runstate)
    except SyncStopped:
        runstate.record_down(runstate.STOPPED)
    except BaseException as exc:
        runstate.record_down(f"kanban sync loop crashed: {type(exc).__name__}: {exc}")
        raise
    finally:
        timing.stop()


def checkpoint() -> None:
    running = _running.get()
    if running is not None and not running():
        raise SyncStopped()
